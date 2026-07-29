import asyncio

from sqlalchemy import select

from app.adapters.base import OddsPayload
from app.adapters.factory import AdapterFactory
from app.core.database import async_session_factory
from app.core.redis import get_redis
from app.fixtures.models import Fixture
from app.fixtures.service import find_fixture_by_abbreviations_and_time
from app.odds.models import Odds
from app.sports.models import League, Sport
from app.workers.celery import celery_app

ODDS_CACHE_TTL_SECONDS = 10 * 60
ODDS_LOOKAHEAD_DAYS = 7


async def _resolve_fixture(db, sport_id, league_id, payload: OddsPayload) -> Fixture | None:
    """Fast path: this odds-provider event was already matched to a fixture on a previous
    run. Otherwise match by team abbreviation + kickoff time and persist the mapping — the
    odds provider (always TheRundown) and the stats/fixtures provider for a sport
    (BallDontLie for NBA) use different ID spaces for the same real-world game; there's no
    shared ID to join on directly (see fixtures.odds_provider_external_id)."""
    fixture = (
        await db.execute(
            select(Fixture).where(
                Fixture.sport_id == sport_id,
                Fixture.odds_provider_external_id == payload.fixture_external_id,
            )
        )
    ).scalar_one_or_none()
    if fixture is not None:
        return fixture

    if payload.kickoff_utc is None:
        return None

    fixture = await find_fixture_by_abbreviations_and_time(
        db,
        sport_id,
        payload.home_team_short_name,
        payload.away_team_short_name,
        payload.kickoff_utc,
        league_id=league_id,
    )
    if fixture is not None and fixture.odds_provider_external_id is None:
        fixture.odds_provider_external_id = payload.fixture_external_id
    return fixture


async def _ingest_odds_for_league(sport: Sport, league: League) -> None:
    adapter = AdapterFactory.get_odds_adapter()
    redis = get_redis()

    async with async_session_factory() as db:
        payloads = await adapter.fetch_odds(
            sport=sport.slug, league=league.slug, days_ahead=ODDS_LOOKAHEAD_DAYS
        )

        for payload in payloads:
            # Events for a game we haven't ingested fixtures for yet (or can't match) are
            # skipped rather than guessed — matches ingest_fixtures.py's dedupe philosophy.
            fixture = await _resolve_fixture(db, sport.id, league.id, payload)
            if fixture is None:
                continue

            db.add(
                Odds(
                    fixture_id=fixture.id,
                    bookmaker=payload.bookmaker,
                    market=payload.market,
                    home_odds=payload.home_odds,
                    draw_odds=payload.draw_odds,
                    away_odds=payload.away_odds,
                    updated_at=payload.updated_at,
                )
            )
            await redis.set(
                f"odds:{fixture.id}:{payload.bookmaker}:{payload.market}",
                payload.home_odds or "",
                ex=ODDS_CACHE_TTL_SECONDS,
            )
        await db.commit()


async def _ingest_odds() -> None:
    async with async_session_factory() as db:
        sports = (await db.execute(select(Sport).where(Sport.active.is_(True)))).scalars().all()
        leagues_by_sport = {
            sport.id: (
                await db.execute(
                    select(League).where(League.sport_id == sport.id, League.active.is_(True))
                )
            )
            .scalars()
            .all()
            for sport in sports
        }

    for sport in sports:
        for league in leagues_by_sport[sport.id]:
            await _ingest_odds_for_league(sport, league)


@celery_app.task(name="app.workers.ingest_odds.ingest_odds")
def ingest_odds() -> None:
    """Celery beat triggers this every 5 minutes for all active sports (TDD §2.3)."""
    asyncio.run(_ingest_odds())
