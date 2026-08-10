"""Capture the pick as it was SHOWN, once per fixture, before kickoff.

Product performance was unmeasurable without this. best_pick is computed per request and never
stored, so grading it after the result would have meant recomputing with today's odds and
today's guards — a different product than users saw. See docs/history-metrics-spec.md §2b.

THE CAPTURE WINDOW IS THE LOAD-BEARING DECISION, and the obvious choice is wrong. Snapshotting
next to capture_closing_odds (T-10 to T-45 min) would make CLV meaningless BY CONSTRUCTION:
CLV is taken/closing - 1, so if the taken price is recorded fifteen minutes before the closing
price, the two are nearly identical and CLV is ~0 no matter how good or bad the model is. The
metric would look reassuringly neutral while measuring nothing.

So the snapshot is taken hours out, where a user would realistically act, leaving room for the
market to move before it closes. That gap IS the measurement.

Cost is proportional to match days rather than to the clock, the same shape as
capture_closing_odds: this makes no external API calls at all — it reads predictions and odds
already in the database — and returns immediately when no fixture sits in the window.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus
from app.predictions.models import PickSnapshot, Prediction
from app.workers.celery import celery_app, run_task

logger = logging.getLogger(__name__)

# Hours before kickoff. Wide enough that an hourly run cannot miss a fixture, and far enough
# from the close that CLV has something to measure — see the module docstring.
SNAPSHOT_WINDOW_START_HOURS = 4
SNAPSHOT_WINDOW_END_HOURS = 8


async def _snapshot_shown_picks() -> None:
    now = datetime.now(UTC)
    window_start = now + timedelta(hours=SNAPSHOT_WINDOW_START_HOURS)
    window_end = now + timedelta(hours=SNAPSHOT_WINDOW_END_HOURS)

    async with async_session_factory() as db:
        fixtures = (
            (
                await db.execute(
                    select(Fixture)
                    .outerjoin(PickSnapshot, PickSnapshot.fixture_id == Fixture.id)
                    .where(
                        Fixture.status == FixtureStatus.SCHEDULED,
                        Fixture.kickoff_utc >= window_start,
                        Fixture.kickoff_utc <= window_end,
                        PickSnapshot.id.is_(None),  # idempotent: never snapshot twice
                    )
                )
            )
            .scalars()
            .all()
        )
        if not fixtures:
            return

        # The SAME function GET /fixtures serves the feed with. Reimplementing the selection
        # here would reintroduce exactly the problem this table exists to solve — measuring
        # something other than what users were shown.
        from app.fixtures.router import _bulk_best_picks

        fixture_ids = [f.id for f in fixtures]
        best_picks, _all_picks = await _bulk_best_picks(db, fixture_ids)

        predictions = {
            row.fixture_id: row
            for row in (
                await db.execute(select(Prediction).where(Prediction.fixture_id.in_(fixture_ids)))
            )
            .scalars()
            .all()
        }

        captured = 0
        for fixture in fixtures:
            pick = best_picks.get(fixture.id)
            prediction = predictions.get(fixture.id)
            if pick is None or prediction is None:
                # No pick was shown, so there is nothing to grade later. Recording a row here
                # would put a fixture into the denominator that never offered a bet.
                continue
            db.add(
                PickSnapshot(
                    fixture_id=fixture.id,
                    prediction_id=prediction.id,
                    model_version=prediction.model_version,
                    market=pick.market,
                    selection=pick.selection,
                    line=pick.line,
                    probability=pick.probability,
                    odds=pick.odds,
                    feature_completeness=prediction.feature_completeness,
                    hours_before_kickoff=(fixture.kickoff_utc - now).total_seconds() / 3600.0,
                )
            )
            captured += 1

        await db.commit()
        logger.info(
            "Captured %d shown pick(s) from %d fixture(s) in the window", captured, len(fixtures)
        )


@celery_app.task(name="app.workers.snapshot_picks.snapshot_shown_picks")
def snapshot_shown_picks() -> None:
    run_task(_snapshot_shown_picks())
