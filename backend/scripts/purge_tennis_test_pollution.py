"""One-off cleanup of pre-2021 tennis fixtures left behind by this feature's own build-out.

While the tennis adapter was being developed, several exploratory ingest runs pulled historical
ATP matches going back to 2007 — long before the 2021-2025 window the model is actually trained
on (see ml/training/collect_tennis_data.py's SEASONS). They serve no product purpose: nothing
trains on them, retrodiction deliberately skips them (backfill_tennis_predictions.py's
RETRODICT_LOOKBACK_DAYS), and they only distort row counts and any future /history rollup.

Deliberately conservative about what "pollution" means: ONLY fixtures whose kickoff predates
the training window are removed. Anything from 2021 onward is real, in-window data and is left
untouched, even if it was also ingested during testing.

Teams (i.e. players) are intentionally NOT deleted. A player who only appears in pre-2021
fixtures leaves behind a harmless orphan row, whereas deleting players risks removing one still
referenced by an in-window fixture — a much worse failure than a few unused rows.

Dependent rows are removed before their fixtures to satisfy the foreign keys. Prints counts and
requires --confirm, since this is destructive and irreversible.

Usage (from backend/):
    PYTHONPATH=. python scripts/purge_tennis_test_pollution.py            # dry run
    PYTHONPATH=. python scripts/purge_tennis_test_pollution.py --confirm  # actually delete
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

# The first season ml/training/collect_tennis_data.py collects. Anything earlier was never
# training data and is not shown anywhere in the product.
TRAINING_WINDOW_START = datetime(2021, 1, 1, tzinfo=UTC)


async def main(confirm: bool) -> None:
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
                        Fixture.kickoff_utc < TRAINING_WINDOW_START,
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
                    Fixture.kickoff_utc >= TRAINING_WINDOW_START,
                )
            )
        ).scalar_one()
        print(
            f"pre-{TRAINING_WINDOW_START:%Y} tennis fixtures to remove: {len(doomed)}\n"
            f"in-window tennis fixtures kept: {kept}"
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
    asyncio.run(main(parser.parse_args().confirm))
