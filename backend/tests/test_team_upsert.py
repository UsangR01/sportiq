"""get_or_create_team against a real Postgres connection (docker-compose) — the first
DB-touching test in this suite, since everything else so far is deliberately DB-free."""

import uuid

import pytest

from app.core.database import async_session_factory
from app.fixtures.service import get_or_create_team
from app.sports.models import League, Sport


@pytest.fixture
async def sport_and_league():
    async with async_session_factory() as db:
        slug = f"test-sport-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Test Sport", model_type="test", active=True)
        db.add(sport)
        await db.flush()

        league = League(
            sport_id=sport.id,
            slug="test-league",
            name="Test League",
            country="XX",
            tier=1,
            active=True,
        )
        db.add(league)
        await db.commit()
        await db.refresh(sport)
        await db.refresh(league)

    yield sport, league

    async with async_session_factory() as db:
        # Cleanup: delete anything the test created, in FK-safe order.
        from sqlalchemy import delete

        from app.fixtures.models import Team

        await db.execute(delete(Team).where(Team.sport_id == sport.id))
        await db.execute(delete(League).where(League.id == league.id))
        await db.execute(delete(Sport).where(Sport.id == sport.id))
        await db.commit()


async def test_get_or_create_team_creates_once_then_reuses(sport_and_league):
    sport, league = sport_and_league

    async with async_session_factory() as db:
        team1 = await get_or_create_team(
            db,
            sport_id=sport.id,
            league_id=league.id,
            external_id="ext-1",
            name="Team One",
            short_name="T1",
        )
        await db.commit()

    async with async_session_factory() as db:
        team2 = await get_or_create_team(
            db,
            sport_id=sport.id,
            league_id=league.id,
            external_id="ext-1",
            name="Renamed Team",  # should be ignored — existing row wins
            short_name="T1",
        )
        await db.commit()

    assert team1.id == team2.id
    assert team2.name == "Team One"
