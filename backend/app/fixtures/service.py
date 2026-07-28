from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.fixtures.models import Team


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
