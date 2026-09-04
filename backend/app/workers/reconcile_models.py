"""Celery-side wrapper around the registry repair, plus the catch-up it implies.

The repair itself is in app/models_ml/registry_repair.py, deliberately free of Celery so the
API can run it at startup without importing a broker it never uses.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory, engine
from app.models_ml.registry_repair import reconcile
from app.sports.models import Sport
from app.workers.celery import celery_app, run_task

logger = logging.getLogger(__name__)


async def _requeue_missing_predictions(db: AsyncSession, sports: list[str]) -> int:
    """Recovery, not routine. A repaired sport has a backlog of upcoming fixtures that were
    queued, failed, and will not be retried until the next daily ingest -- up to 24 hours of a
    visibly empty feed after the fix has already landed.

    Bounded by the same window ingest_fixtures uses, and fires only for a sport that was
    actually repaired, so an ordinary restart queues nothing.
    """
    from app.fixtures.models import Fixture, FixtureStatus
    from app.predictions.models import Prediction
    from app.workers.ingest_fixtures import FEATURE_LOOKAHEAD_DAYS

    now = datetime.now(UTC)
    rows = (
        await db.execute(
            select(Fixture.id)
            .join(Sport, Sport.id == Fixture.sport_id)
            .where(
                Sport.slug.in_(sports),
                Fixture.status == FixtureStatus.SCHEDULED,
                Fixture.kickoff_utc >= now,
                Fixture.kickoff_utc <= now + timedelta(days=FEATURE_LOOKAHEAD_DAYS),
                ~select(Prediction.id).where(Prediction.fixture_id == Fixture.id).exists(),
            )
        )
    ).all()

    # Dispatched BY NAME so this module never imports the prediction task -- and with it
    # xgboost -- into whatever process is doing the reconciling.
    for (fixture_id,) in rows:
        celery_app.send_task("app.workers.run_predictions.run_predictions", args=[str(fixture_id)])
    return len(rows)


async def _reconcile_models() -> None:
    async with async_session_factory() as db:
        repaired = await reconcile(db)
        if not repaired:
            logger.info("serving models reconciled - nothing to repair")
            return
        queued = await _requeue_missing_predictions(db, repaired)
        logger.error("REPAIRED %s and queued %d catch-up predictions", ", ".join(repaired), queued)
    await engine.dispose()


@celery_app.task(name="app.workers.reconcile_models.reconcile_models")
def reconcile_models() -> None:
    run_task(_reconcile_models())
