"""Cleanup of out-of-window tennis fixtures written by unfiltered historical ingest.

Run twice, for two different causes:

  Round 1 (pre-2021)  Exploratory ingest runs during the adapter's build-out pulled ATP matches
                      back to 2007. Removed 2,729 fixtures.

  Round 2 (pre-2026)  The real, ongoing cause, found later: a tournament id identifies the
                      EVENT, not one year's running of it, so /matches?tournament_ids[]=X
                      returned every edition ever played. A routine +/-2 day poll was writing
                      2,616 fixtures spanning 2007-2026 where 119 were current. Round 1 had
                      read the leftovers as one-off test data; they were being recreated on a
                      schedule. Fixed at source in balldontlie_tennis.py:_is_same_edition —
                      run this only after that fix, or the rows come straight back.

Nothing depends on these rows, verified rather than assumed: tennis training reads
ml/data/tennis_*.parquet and never the DB (train_tennis.py), retrodiction looks back only
RETRODICT_LOOKBACK_DAYS (60), and GET /history is still a 501 stub.

The deeper reason not to keep them is that they are not a coherent historical record. Which
matches were written depended on which tournaments happened to overlap a poll window, so the
data is an arbitrary subset — 134 matches for 2007 against a real ATP season of thousands.
A future /history rollup over that would report confident, wrong numbers; the absence of data
is easier to notice than a biased sample of it.

Teams (i.e. players) are intentionally NOT deleted. A player appearing only in removed fixtures
leaves a harmless orphan row, whereas deleting players risks removing one still referenced by an
in-window fixture — a much worse failure than a few unused rows.

Dependent rows are removed before their fixtures to satisfy the foreign keys. Prints counts and
requires --confirm, since this is destructive and irreversible.

Usage (from backend/):
    PYTHONPATH=. python scripts/purge_tennis_test_pollution.py                    # dry run
    PYTHONPATH=. python scripts/purge_tennis_test_pollution.py --confirm          # delete
    PYTHONPATH=. python scripts/purge_tennis_test_pollution.py --before 2025-01-01
"""

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import delete, func, select

from app.core.database import async_session_factory, engine
from app.fixtures.models import Fixture, FixtureLiveState, TeamFeatures
from app.history.models import Outcome
from app.predictions.models import Prediction
from app.sports.models import Sport

# Default cutoff: the start of the current season. Everything earlier was written by the
# unfiltered-edition bug and is unreachable from the product — the feed browses by day, and
# nothing else reads a tennis fixture older than RETRODICT_LOOKBACK_DAYS. Overridable via
# --before so the blast radius is always an explicit, stated date rather than a constant
# someone has to go and read.
DEFAULT_CUTOFF = datetime(2026, 1, 1, tzinfo=UTC)


async def main(confirm: bool, cutoff: datetime) -> None:
    async with async_session_factory() as db:
        sport = (await db.execute(select(Sport).where(Sport.slug == "tennis"))).scalar_one_or_none()
        if sport is None:
            print("no tennis sport seeded — nothing to do")
            return

        doomed = (
            (
                await db.execute(
                    select(Fixture.id).where(
                        Fixture.sport_id == sport.id,
                        Fixture.kickoff_utc < cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        kept = (
            await db.execute(
                select(func.count())
                .select_from(Fixture)
                .where(
                    Fixture.sport_id == sport.id,
                    Fixture.kickoff_utc >= cutoff,
                )
            )
        ).scalar_one()
        print(
            f"tennis fixtures with a kickoff before {cutoff:%Y-%m-%d} to remove: {len(doomed)}\n"
            f"tennis fixtures kept: {kept}"
        )
        if not doomed:
            return
        if not confirm:
            print("dry run — re-run with --confirm to actually delete")
            return

        # Children first: predictions/live state/outcomes/features all FK onto fixtures.
        for model, label in (
            (Prediction, "predictions"),
            (FixtureLiveState, "live-state rows"),
            (Outcome, "outcomes"),
            (TeamFeatures, "team-feature rows"),
        ):
            result = await db.execute(delete(model).where(model.fixture_id.in_(doomed)))
            print(f"  deleted {result.rowcount} {label}")
        result = await db.execute(delete(Fixture).where(Fixture.id.in_(doomed)))
        print(f"  deleted {result.rowcount} fixtures")
        await db.commit()
        print("done")
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="actually perform the deletion")
    parser.add_argument(
        "--before",
        default=DEFAULT_CUTOFF.strftime("%Y-%m-%d"),
        help="ISO date; remove tennis fixtures with a kickoff STRICTLY before it",
    )
    args = parser.parse_args()
    asyncio.run(main(args.confirm, datetime.fromisoformat(args.before).replace(tzinfo=UTC)))
