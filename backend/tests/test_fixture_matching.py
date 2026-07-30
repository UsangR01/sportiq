"""find_fixture_by_abbreviations_and_time against a real Postgres connection (docker-compose)
— same DB-touching pattern as test_team_upsert.py."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.fixtures.service import find_fixture_by_abbreviations_and_time
from app.sports.models import League, Sport


@pytest.fixture
async def seeded_fixture():
    kickoff = datetime(2026, 1, 16, 0, 0, tzinfo=UTC)

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
        await db.flush()

        home = Team(
            sport_id=sport.id,
            league_id=league.id,
            name="Home Team",
            short_name="HOM",
            external_id="home-1",
        )
        away = Team(
            sport_id=sport.id,
            league_id=league.id,
            name="Away Team",
            short_name="AWY",
            external_id="away-1",
        )
        db.add_all([home, away])
        await db.flush()

        fixture = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            external_id="fixture-1",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=kickoff,
            status=FixtureStatus.SCHEDULED,
            season="2025",
        )
        db.add(fixture)
        await db.commit()
        await db.refresh(sport)
        await db.refresh(fixture)

    yield sport, fixture, kickoff

    async with async_session_factory() as db:
        await db.execute(delete(Fixture).where(Fixture.sport_id == sport.id))
        await db.execute(delete(Team).where(Team.sport_id == sport.id))
        await db.execute(delete(League).where(League.sport_id == sport.id))
        await db.execute(delete(Sport).where(Sport.id == sport.id))
        await db.commit()


async def test_find_fixture_matches_within_tolerance(seeded_fixture):
    sport, fixture, kickoff = seeded_fixture
    async with async_session_factory() as db:
        found = await find_fixture_by_abbreviations_and_time(
            db,
            sport.id,
            home_abbreviation="HOM",
            away_abbreviation="AWY",
            kickoff_utc=kickoff + timedelta(minutes=10),
        )
    assert found is not None
    assert found.id == fixture.id


async def test_find_fixture_returns_none_outside_tolerance(seeded_fixture):
    sport, _fixture, kickoff = seeded_fixture
    async with async_session_factory() as db:
        found = await find_fixture_by_abbreviations_and_time(
            db,
            sport.id,
            home_abbreviation="HOM",
            away_abbreviation="AWY",
            kickoff_utc=kickoff + timedelta(days=3),
            tolerance_hours=24,
        )
    assert found is None


async def test_find_fixture_returns_none_for_unknown_abbreviation(seeded_fixture):
    sport, _fixture, kickoff = seeded_fixture
    async with async_session_factory() as db:
        found = await find_fixture_by_abbreviations_and_time(
            db, sport.id, home_abbreviation="ZZZ", away_abbreviation="AWY", kickoff_utc=kickoff
        )
    assert found is None


async def test_find_fixture_returns_none_for_missing_abbreviation():
    async with async_session_factory() as db:
        found = await find_fixture_by_abbreviations_and_time(
            db,
            uuid.uuid4(),
            home_abbreviation=None,
            away_abbreviation="AWY",
            kickoff_utc=datetime.now(UTC),
        )
    assert found is None


async def test_find_fixture_returns_none_instead_of_crashing_on_ambiguous_short_name(
    seeded_fixture,
):
    """Confirmed live: API-Football's own team code isn't unique within a league (Colorado
    Rapids and Columbus Crew both code to "COL" in real MLS data — see CLAUDE.md). A second
    real team sharing the home team's short_name must degrade to "no confident match", not
    raise MultipleResultsFound and crash the whole odds-ingestion run for that league."""
    sport, fixture, kickoff = seeded_fixture
    async with async_session_factory() as db:
        db.add(
            Team(
                sport_id=sport.id,
                league_id=fixture.league_id,
                name="Home Team Duplicate",
                short_name="HOM",
                external_id="home-2",
            )
        )
        await db.commit()

    async with async_session_factory() as db:
        found = await find_fixture_by_abbreviations_and_time(
            db,
            sport.id,
            home_abbreviation="HOM",
            away_abbreviation="AWY",
            kickoff_utc=kickoff,
        )
    assert found is None
