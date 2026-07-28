import asyncio

from sqlalchemy import select

from app.adapters.base import TeamStats
from app.adapters.factory import AdapterFactory
from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, Team, TeamFeatures
from app.fixtures.service import get_or_create_team
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

        for payload in fixture_payloads:
            # Dedupe on the provider's own fixture ID, not the internal UUID PK — matching on
            # Fixture.id here would never hit (see CLAUDE.md for why this was previously wrong).
            existing = (
                await db.execute(
                    select(Fixture).where(
                        Fixture.sport_id == sport.id, Fixture.external_id == payload.external_id
                    )
                )
            ).scalar_one_or_none()

            home_team = await get_or_create_team(
                db,
                sport_id=sport.id,
                league_id=league.id,
                external_id=payload.home_team_external_id,
                name=payload.home_team_name or payload.home_team_external_id,
                short_name=payload.home_team_short_name,
            )
            away_team = await get_or_create_team(
                db,
                sport_id=sport.id,
                league_id=league.id,
                external_id=payload.away_team_external_id,
                name=payload.away_team_name or payload.away_team_external_id,
                short_name=payload.away_team_short_name,
            )

            if existing is None:
                db.add(
                    Fixture(
                        sport_id=sport.id,
                        league_id=league.id,
                        external_id=payload.external_id,
                        home_team_id=home_team.id,
                        away_team_id=away_team.id,
                        kickoff_utc=payload.kickoff_utc,
                        status=FixtureStatus(payload.status),
                        season=payload.season,
                    )
                )
            else:
                # TODO: TDD §2.3 says "cancelled/postponed fixtures are updated", but the
                # fixtures.status enum in §2.1 only defines scheduled|live|completed — no
                # cancelled/postponed value. Status transitions we DO support get applied here.
                new_status = FixtureStatus(payload.status)
                if existing.status != new_status:
                    existing.status = new_status
        await db.commit()

        # Team feature vectors, computed at ingest time for fixtures in the next 7 days
        # (TDD §2.3) — last 10 matches, xG, H2H via the stats adapter.
        upcoming = (
            (await db.execute(select(Fixture).where(Fixture.league_id == league.id)))
            .scalars()
            .all()
        )
        # A team appears in many fixtures within one run (every fixture it plays in this
        # league), and fetch_team_stats is expensive/rate-limited — cache per external_id so
        # each team is only fetched once per run instead of once per (fixture, team) pair.
        team_stats_cache: dict[str, TeamStats] = {}
        for fixture in upcoming:
            for team_id in (fixture.home_team_id, fixture.away_team_id):
                # fetch_team_stats needs the provider's own team ID, not our internal UUID —
                # passing team_id directly here would silently query the real API with a
                # UUID it doesn't recognise (caught while writing this, not from experience).
                team = (
                    await db.execute(select(Team).where(Team.id == team_id))
                ).scalar_one_or_none()
                if team is None or team.external_id is None:
                    continue
                if team.external_id not in team_stats_cache:
                    team_stats_cache[team.external_id] = await adapter.fetch_team_stats(
                        team.external_id, n_matches=FEATURE_WINDOW_MATCHES
                    )
                stats = team_stats_cache[team.external_id]
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
                        season_point_diff=stats.season_point_diff,
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
