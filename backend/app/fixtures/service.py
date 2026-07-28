from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.fixtures.models import Fixture, Team


async def get_or_create_team(
    db: AsyncSession,
    sport_id,
    league_id,
    external_id: str,
    name: str,
    short_name: str | None = None,
) -> Team:
    """Look up a Team by (sport_id, external_id); create it if this is the first time this
    provider ID has been seen. Needed because fixture payloads carry provider IDs, not our
    internal UUIDs — see CLAUDE.md's ingest_fixtures notes for why this exists."""
    existing = (
        await db.execute(
            select(Team).where(Team.sport_id == sport_id, Team.external_id == external_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    team = Team(
        sport_id=sport_id,
        league_id=league_id,
        name=name,
        short_name=short_name,
        external_id=external_id,
    )
    db.add(team)
    await db.flush()  # populate team.id without committing the outer transaction
    return team


async def find_fixture_by_abbreviations_and_time(
    db: AsyncSession,
    sport_id,
    home_abbreviation: str | None,
    away_abbreviation: str | None,
    kickoff_utc: datetime,
    tolerance_hours: int = 24,
) -> Fixture | None:
    """Matches an odds-provider event to one of our existing fixtures by team short_name
    (abbreviation) plus a kickoff-time window — the odds provider (always TheRundown, per
    TDD §6.2) and the stats/fixtures provider (BallDontLie for NBA, API-Football for
    football) use different ID spaces for the same real-world game; there's no shared ID to
    join on directly. Returns None (never guesses) if either abbreviation is missing or
    nothing matches — the caller should skip that event rather than mis-attribute odds."""
    if not home_abbreviation or not away_abbreviation:
        return None

    home_team = (
        await db.execute(
            select(Team).where(Team.sport_id == sport_id, Team.short_name == home_abbreviation)
        )
    ).scalar_one_or_none()
    away_team = (
        await db.execute(
            select(Team).where(Team.sport_id == sport_id, Team.short_name == away_abbreviation)
        )
    ).scalar_one_or_none()
    if home_team is None or away_team is None:
        return None

    window = timedelta(hours=tolerance_hours)
    return (
        await db.execute(
            select(Fixture).where(
                Fixture.sport_id == sport_id,
                Fixture.home_team_id == home_team.id,
                Fixture.away_team_id == away_team.id,
                Fixture.kickoff_utc.between(kickoff_utc - window, kickoff_utc + window),
            )
        )
    ).scalar_one_or_none()
