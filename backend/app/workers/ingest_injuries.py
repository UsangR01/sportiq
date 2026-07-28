import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.adapters.balldontlie import BallDontLieAdapter
from app.adapters.rotowire import RotoWireAdapter
from app.core.config import get_settings
from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, PlayerInjuryStatus
from app.sports.models import Sport
from app.workers.celery import celery_app

logger = logging.getLogger(__name__)

RE_INFERENCE_WINDOW_HOURS = 3
HIGH_PRIORITY_SALARY_RANK = 5  # "salary rank ≤ 5 on roster" (TDD §3.3)


async def _has_active_nba_fixtures_today(db, sport_id) -> bool:
    now = datetime.now(UTC)
    end_of_day = now.replace(hour=23, minute=59, second=59)
    result = await db.execute(
        select(Fixture.id).where(
            Fixture.sport_id == sport_id,
            Fixture.status != FixtureStatus.COMPLETED,
            Fixture.kickoff_utc.between(now, end_of_day),
        )
    )
    return result.first() is not None


async def _ingest_injuries_rotowire(db, sport: Sport) -> None:
    """RotoWire path (TDD §2.3): only runs on NBA game days; filtered to high-priority
    OUT/GTD; a high-priority OUT within 3 hours of tip-off re-runs model inference."""
    if not await _has_active_nba_fixtures_today(db, sport.id):
        return

    adapter = RotoWireAdapter()
    updates = await adapter.fetch_injuries(sport=sport.slug)

    now = datetime.now(UTC)
    for update in updates:
        db.add(
            PlayerInjuryStatus(
                sport_id=sport.id,
                player_id=update.player_external_id,
                team_id=update.team_external_id,
                player_name=update.player_name,
                status=update.status,
                return_date=update.return_date,
                salary_rank=update.salary_rank,
                source=update.source,
                updated_at=now,
            )
        )

        is_high_priority_out = update.status == "OUT" and (
            update.salary_rank is not None and update.salary_rank <= HIGH_PRIORITY_SALARY_RANK
        )
        if is_high_priority_out:
            # Re-inference trigger is RotoWire-only — disabled in the BallDontLie fallback path.
            # Actual fixture-within-3-hours check + run_predictions dispatch: not yet wired up,
            # since run_predictions itself has no trained model to call (app/models_ml).
            logger.info(
                "High-priority OUT for %s — re-inference trigger not yet wired to run_predictions",
                update.player_name,
            )

    await db.commit()


async def _ingest_injuries_balldontlie(db, sport: Sport) -> None:
    """BallDontLie fallback path (TDD §2.3): used automatically when ROTOWIRE_API_KEY is
    absent. Less real-time (no GTD → OUT alerts); re-inference trigger stays disabled."""
    adapter = BallDontLieAdapter()
    updates = await adapter.fetch_injuries(sport=sport.slug)

    now = datetime.now(UTC)
    for update in updates:
        db.add(
            PlayerInjuryStatus(
                sport_id=sport.id,
                player_id=update.player_external_id,
                team_id=update.team_external_id,
                player_name=update.player_name,
                status=update.status,
                return_date=update.return_date,
                salary_rank=update.salary_rank,
                source=update.source,
                updated_at=now,
            )
        )
    await db.commit()


async def _ingest_injuries() -> None:
    settings = get_settings()

    async with async_session_factory() as db:
        nba = (await db.execute(select(Sport).where(Sport.slug == "nba"))).scalar_one_or_none()
        if nba is None:
            return

        if not settings.rotowire_api_key:
            logger.warning("ROTOWIRE_API_KEY not set — using BallDontLie fallback")
            await _ingest_injuries_balldontlie(db, nba)
        else:
            await _ingest_injuries_rotowire(db, nba)


@celery_app.task(name="app.workers.ingest_injuries.ingest_injuries")
def ingest_injuries() -> None:
    """Celery beat triggers this every 30 minutes; the RotoWire path additionally gates on
    NBA game days (TDD §2.3). NBA only at MVP."""
    asyncio.run(_ingest_injuries())
