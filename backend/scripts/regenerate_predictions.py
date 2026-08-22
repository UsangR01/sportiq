"""Regenerate predictions for upcoming fixtures so they carry driver explanations.

WHY THIS IS NEEDED AT ALL

`predictions.driver_contributions` is written at inference time and cannot be backfilled -- the
contributions decompose the feature vector as it stood at that instant, and rebuilding it later
would explain a fixture with form and odds that had not happened yet.

Normally that self-corrects, because `ingest_fixtures` re-queues any fixture whose stored
`model_version` differs from the ACTIVE one. It does not correct here: the market-blind artefact
is deliberately `is_active=False`, so the active version never changed and the re-queue rule
never fires. Every prediction made before attribution landed keeps a NULL explanation for good,
and only fixtures predicted fresh from now on pick one up.

So this is a ONE-OFF nudge, not a scheduled job. Run it once after the blind artefact ships; new
fixtures need nothing.

WHAT IT COSTS

One real API-Football `/fixtures/headtohead` call per football fixture, paced. Nothing else --
team stats and odds are already in the database, and the blind model is loaded from the image.

RUN IT WITH --queue ON RENDER. MEASURED, BECAUSE THIS KILLED THE SHELL TWICE:

    imports alone (xgboost, pandas, sklearn)      189 MB
    after one prediction (two models loaded)      311 MB
    plateau across eight fixtures                 326 MB

The Render shell runs INSIDE the web service's container, so all of that lands on top of the
live API process. A smaller --limit does not help: the floor is the imports and the models, not
the loop, so a single fixture costs nearly as much as fifty.

`--queue` instead dispatches each fixture to the Celery worker by task NAME, which needs only
Redis -- no xgboost, no pandas, no model. The worker is a separate service that already holds
those loaded because running predictions is its job. It also means a dropped shell no longer
matters: the work is already queued and continues without it.

    PYTHONPATH=. python scripts/regenerate_predictions.py                  # dry run, safe
    PYTHONPATH=. python scripts/regenerate_predictions.py --confirm --queue # ON RENDER
    PYTHONPATH=. python scripts/regenerate_predictions.py --confirm         # inline, locally
"""

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.database import async_session_factory, engine
from app.fixtures.models import Fixture, FixtureStatus
from app.predictions.models import Prediction
from app.sports.models import Sport

#: Matches FEATURE_LOOKAHEAD_DAYS in ingest_fixtures -- there is no point predicting a fixture
#: further out than the feed will show, and its features would be regenerated before kickoff
#: anyway.
LOOKAHEAD_DAYS = 7

#: Between fixtures. API-Football's Ultra plan allows far more than this needs, but a burst is
#: what produced the 429-then-spurious-401 cascade documented for the other providers, and this
#: script has no deadline worth hurrying for.
REQUEST_DELAY_SECONDS = 1.0

#: A backstop, not a tuning knob. A runaway loop against a metered API is the failure worth
#: making impossible, and no real run needs more than this.
MAX_FIXTURES = 500


async def _fixtures_needing_explanations(db, sport_slug: str, limit: int) -> list[tuple]:
    """Upcoming fixtures whose CURRENT best prediction has no contributions stored.

    Deliberately keyed on the newest prediction rather than on "any prediction lacking them":
    the table keeps every revision, so older rows will always be missing contributions and
    would make this script look permanently unfinished.
    """
    now = datetime.now(UTC)
    fixtures = (
        (
            await db.execute(
                select(Fixture)
                .join(Sport, Sport.id == Fixture.sport_id)
                .where(
                    Sport.slug == sport_slug,
                    Fixture.status == FixtureStatus.SCHEDULED,
                    Fixture.kickoff_utc > now,
                    Fixture.kickoff_utc < now + timedelta(days=LOOKAHEAD_DAYS),
                )
                .order_by(Fixture.kickoff_utc)
            )
        )
        .scalars()
        .all()
    )

    needing: list[tuple] = []
    for fixture in fixtures:
        newest = (
            await db.execute(
                select(Prediction)
                .where(Prediction.fixture_id == fixture.id)
                .order_by(Prediction.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        # No prediction at all is left alone: ingest_fixtures already queues those, and doing it
        # here too would duplicate work the scheduled path owns.
        if newest is None or newest.driver_contributions is not None:
            continue
        needing.append((fixture, newest))
        if len(needing) >= limit:
            break
    return needing


async def main(confirm: bool, sport_slug: str, limit: int, queue: bool) -> None:
    async with async_session_factory() as db:
        targets = await _fixtures_needing_explanations(db, sport_slug, limit)

    if not targets:
        print(f"no upcoming {sport_slug} fixtures need regenerating - nothing to do")
        await engine.dispose()
        return

    print(f"{len(targets)} {sport_slug} fixtures whose newest prediction has no explanation:\n")
    for fixture, prediction in targets[:10]:
        print(
            f"  {fixture.kickoff_utc:%Y-%m-%d %H:%M}  {str(fixture.id)[:8]}  "
            f"on {prediction.model_version}"
        )
    if len(targets) > 10:
        print(f"  ... and {len(targets) - 10} more")

    print(f"\ncost: ~{len(targets)} API-Football head-to-head calls, one per fixture")
    if not confirm:
        print("dry run - re-run with --confirm to regenerate")
        await engine.dispose()
        return

    if queue:
        # Dispatched BY NAME rather than by importing the task, which is the entire point:
        # importing app.workers.run_predictions pulls the model runner and therefore xgboost,
        # and it is that import -- not the work -- that makes this too heavy for the shell.
        from app.workers.celery import celery_app

        for fixture, _ in targets:
            celery_app.send_task(
                "app.workers.run_predictions.run_predictions", args=[str(fixture.id)]
            )
        print(f"queued {len(targets)} fixtures to the Celery worker")
        print(
            "  the worker processes these on its own; re-run this script (without --confirm) "
            "in a few minutes to see the remaining count fall"
        )
        await engine.dispose()
        return

    # Inline. Fine locally; see the module docstring for why not on Render.
    from app.workers.run_predictions import _run_predictions

    done = failed = 0
    for index, (fixture, _) in enumerate(targets, start=1):
        try:
            await _run_predictions(fixture.id)
            done += 1
        except Exception as exc:  # noqa: BLE001
            # One bad fixture must not abandon the rest -- a missing team stat or a transient
            # provider error is ordinary, and the run is resumable but not free.
            failed += 1
            print(f"  FAILED {str(fixture.id)[:8]}: {type(exc).__name__}: {exc}")
        if index % 10 == 0 or index == len(targets):
            print(f"  {index}/{len(targets)}  ({done} regenerated, {failed} failed)")
        if index < len(targets):
            await asyncio.sleep(REQUEST_DELAY_SECONDS)

    # Report what actually LANDED rather than what was attempted: a prediction can be written
    # while attribution returns None (no blind artefact staged for the sport, or a load error),
    # and that failure is silent by design so it cannot cost a fixture its prediction.
    async with async_session_factory() as db:
        with_drivers = 0
        for fixture, _ in targets:
            newest = (
                await db.execute(
                    select(Prediction)
                    .where(Prediction.fixture_id == fixture.id)
                    .order_by(Prediction.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if newest is not None and newest.driver_contributions is not None:
                with_drivers += 1

    print(f"\n{done} regenerated, {failed} failed")
    print(f"{with_drivers}/{len(targets)} now carry a driver explanation")
    if with_drivers < done:
        print(
            "  some predictions were written without one - check that the market-blind artefact "
            "is present in this image (ml/artifacts/deployed/) and registered in models_registry"
        )
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true", help="actually regenerate")
    parser.add_argument(
        "--sport",
        default="football",
        help="sport slug (default: football - the only sport with a market-blind artefact; "
        "tennis and NBA explain themselves and get contributions on their next prediction "
        "without needing this)",
    )
    parser.add_argument("--limit", type=int, default=MAX_FIXTURES)
    parser.add_argument(
        "--queue",
        action="store_true",
        help="dispatch to the Celery worker instead of predicting inline. USE THIS ON RENDER - "
        "the shell shares the web service's container and the inline path needs ~330MB, which "
        "OOMs it. Also survives the shell dropping, since the work is already queued.",
    )
    args = parser.parse_args()
    asyncio.run(main(args.confirm, args.sport, min(args.limit, MAX_FIXTURES), args.queue))
