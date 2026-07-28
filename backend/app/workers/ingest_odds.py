import asyncio

from sqlalchemy import select

from app.adapters.factory import AdapterFactory
from app.core.database import async_session_factory
from app.core.redis import get_redis
from app.odds.models import Odds
from app.sports.models import Sport
from app.workers.celery import celery_app

ODDS_CACHE_TTL_SECONDS = 10 * 60


async def _ingest_odds_for_sport(sport: Sport) -> None:
    adapter = AdapterFactory.get_odds_adapter()
    redis = get_redis()

    async with async_session_factory() as db:
        # Real fixture-id resolution (external -> internal) isn't implemented yet — this task
        # is a scaffold until TheRundownAdapter.fetch_odds is implemented (TDD §2.3).
        payloads = await adapter.fetch_odds(fixture_ids=[])

        for payload in payloads:
            db.add(
                Odds(
                    fixture_id=payload.fixture_external_id,
                    bookmaker=payload.bookmaker,
                    market=payload.market,
                    home_odds=payload.home_odds,
                    draw_odds=payload.draw_odds,
                    away_odds=payload.away_odds,
                    updated_at=payload.updated_at,
                )
            )
            await redis.set(
                f"odds:{payload.fixture_external_id}:{payload.bookmaker}:{payload.market}",
                payload.home_odds or "",
                ex=ODDS_CACHE_TTL_SECONDS,
            )
        await db.commit()


async def _ingest_odds() -> None:
    async with async_session_factory() as db:
        sports = (await db.execute(select(Sport).where(Sport.active.is_(True)))).scalars().all()
    for sport in sports:
        await _ingest_odds_for_sport(sport)


@celery_app.task(name="app.workers.ingest_odds.ingest_odds")
def ingest_odds() -> None:
    """Celery beat triggers this every 5 minutes for all active sports (TDD §2.3)."""
    asyncio.run(_ingest_odds())
