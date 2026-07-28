import asyncio

from sqlalchemy import select

from app.adapters.factory import AdapterFactory
from app.core.database import async_session_factory
from app.fixtures.models import Fixture, TeamFeatures
from app.sports.models import League, Sport
from app.workers.celery import celery_app

FEATURE_LOOKAHEAD_DAYS = 7
FEATURE_WINDOW_MATCHES = 10


async def _ingest_fixtures_for_league(sport: Sport, league: League) -> None:
    # NOTE: TDD §6.2 references a sports.data_source_slug column that isn't in the §2.1 schema
    # listing. Using sport.slug directly as the AdapterFactory key until that's reconciled.
    adapter = AdapterFactory.get_stats_adapter(sport.slug)

    async with async_session_factory() as db:
        fixture_payloads = await adapter.fetch_fixtures(
            sport=sport.slug, league=league.slug, days_ahead=FEATURE_LOOKAHEAD_DAYS
        )

        # TODO: TDD §2.3 says "cancelled/postponed fixtures are updated", but the fixtures.status
        # enum in §2.1 only defines scheduled|live|completed — no cancelled/postponed value.
        # Insert-new / update-existing logic goes here once fetch_fixtures is implemented.
        for payload in fixture_payloads:
            existing = (
                await db.execute(select(Fixture).where(Fixture.id == payload.external_id))
            ).scalar_one_or_none()
            if existing is None:
                db.add(
                    Fixture(
                        sport_id=sport.id,
                        league_id=league.id,
                        home_team_id=payload.home_team_external_id,
                        away_team_id=payload.away_team_external_id,
                        kickoff_utc=payload.kickoff_utc,
                        season=payload.season,
                    )
                )
        await db.commit()

        # Team feature vectors, computed at ingest time for fixtures in the next 7 days
        # (TDD §2.3) — last 10 matches, xG, H2H via the stats adapter.
        upcoming = (
            (await db.execute(select(Fixture).where(Fixture.league_id == league.id)))
            .scalars()
            .all()
        )
        for fixture in upcoming:
            for team_id in (fixture.home_team_id, fixture.away_team_id):
                stats = await adapter.fetch_team_stats(
                    str(team_id), n_matches=FEATURE_WINDOW_MATCHES
                )
                db.add(
                    TeamFeatures(
                        team_id=team_id,
                        fixture_id=fixture.id,
                        elo_rating=stats.elo_rating,
                        attack_str=stats.attack_str,
                        defence_str=stats.defence_str,
                        form_pts_5=stats.form_pts_5,
                        xg_for_5=stats.xg_for_5,
                        xg_against_5=stats.xg_against_5,
                        days_since_last_match=stats.days_since_last_match,
                        home_win_rate=stats.home_win_rate,
                        away_win_rate=stats.away_win_rate,
                    )
                )
        await db.commit()


async def _ingest_fixtures() -> None:
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
            await _ingest_fixtures_for_league(sport, league)


@celery_app.task(name="app.workers.ingest_fixtures.ingest_fixtures")
def ingest_fixtures() -> None:
    """Celery beat triggers this daily at 02:00 UTC (TDD §2.3)."""
    asyncio.run(_ingest_fixtures())
