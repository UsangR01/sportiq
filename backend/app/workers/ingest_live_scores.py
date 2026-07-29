"""Live score polling (TDD §2.3) — previously an unreachable stub because TheRundown's own
scores endpoint doesn't map onto any DataSourceAdapter ABC method (see the module's prior
history in git). Real now via a different route: fetch_fixtures (the stats/fixtures
adapter — API-Football for football, BallDontLie for NBA) already returns live goals/status
for any fixture in its queried date range, since that's the same endpoint ingest_fixtures.py
uses for the daily backfill. Re-querying a narrow window around "now" every 5 minutes catches
score/status changes for fixtures already in our DB, without needing a dedicated live-scores
adapter method at all.
"""

import asyncio

from sqlalchemy import select

from app.adapters.factory import AdapterFactory
from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus
from app.sports.models import League, Sport
from app.workers.celery import celery_app
from app.workers.ingest_fixtures import _maybe_settle_outcome, _upsert_live_state

# +/-1 day around "now" — wide enough to catch a fixture that kicked off late yesterday (UTC)
# and is still in progress, or one about to start in the next few hours, without re-querying
# a fixture's provider-side data any more often than every 5 minutes (TDD §2.3's schedule).
LIVE_SCORES_WINDOW_DAYS = 1


async def _ingest_live_scores_for_league(sport: Sport, league: League) -> None:
    adapter = AdapterFactory.get_stats_adapter(sport.slug)

    async with async_session_factory() as db:
        payloads = await adapter.fetch_fixtures(
            sport=sport.slug,
            league=league.slug,
            days_ahead=LIVE_SCORES_WINDOW_DAYS,
            days_back=LIVE_SCORES_WINDOW_DAYS,
        )

        for payload in payloads:
            # Only update fixtures we already know about — a fixture this poll discovers for
            # the first time is ingest_fixtures.py's job (team creation, feature computation),
            # not this one's; it'll be picked up on the next daily run.
            fixture = (
                await db.execute(
                    select(Fixture).where(
                        Fixture.sport_id == sport.id, Fixture.external_id == payload.external_id
                    )
                )
            ).scalar_one_or_none()
            if fixture is None:
                continue

            new_status = FixtureStatus(payload.status)
            if fixture.status != new_status:
                fixture.status = new_status

            await _upsert_live_state(db, fixture.id, payload)
            await _maybe_settle_outcome(db, fixture.id, payload)

        await db.commit()


async def _ingest_live_scores() -> None:
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
            await _ingest_live_scores_for_league(sport, league)


@celery_app.task(name="app.workers.ingest_live_scores.ingest_live_scores")
def ingest_live_scores() -> None:
    """Celery beat triggers this every 5 minutes, alongside odds ingest (TDD §2.3)."""
    asyncio.run(_ingest_live_scores())
