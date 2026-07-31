"""app/fixtures/router.py:_fetch_head_to_head — the real H2H panel that replaced the raw
bookmaker-odds table on the fixture detail screen per direct user request ("Users don't find
the Odds section useful... replaced with H2H statistics"). Tested directly (not through the
full GET /fixtures/{id} HTTP endpoint) since _fetch_head_to_head takes sport_slug as an
explicit parameter, decoupled from whatever the seeded test Sport row's own slug is — avoids
needing a real Sport row literally slugged "football" (which already exists, uniquely, in the
real dev DB this test suite runs against)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.adapters.api_football import H2HDetail
from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.fixtures.router import _fetch_head_to_head
from app.sports.models import League, Sport


@pytest.fixture
async def seeded_fixture_with_external_ids():
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
        home = Team(sport_id=sport.id, league_id=league.id, name="Home FC", external_id="100")
        away = Team(sport_id=sport.id, league_id=league.id, name="Away FC", external_id="200")
        db.add_all([home, away])
        await db.flush()
        fixture = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            external_id="fx-h2h-1",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=datetime.now(UTC) + timedelta(days=1),
            status=FixtureStatus.SCHEDULED,
            season="2026",
        )
        db.add(fixture)
        await db.commit()
        await db.refresh(fixture)

    yield fixture

    async with async_session_factory() as db:
        await db.execute(delete(Fixture).where(Fixture.id == fixture.id))
        await db.execute(delete(Team).where(Team.league_id == league.id))
        await db.execute(delete(League).where(League.id == league.id))
        await db.execute(delete(Sport).where(Sport.id == sport.id))
        await db.commit()


@pytest.fixture
async def seeded_fixture_without_external_ids():
    """Teams with no external_id at all (e.g. never resolved to a real provider ID) — must
    degrade to None, not crash trying to call the live adapter with a None ID."""
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
        home = Team(sport_id=sport.id, league_id=league.id, name="Home FC")
        away = Team(sport_id=sport.id, league_id=league.id, name="Away FC")
        db.add_all([home, away])
        await db.flush()
        fixture = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            external_id="fx-h2h-2",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=datetime.now(UTC) + timedelta(days=1),
            status=FixtureStatus.SCHEDULED,
            season="2026",
        )
        db.add(fixture)
        await db.commit()
        await db.refresh(fixture)

    yield fixture

    async with async_session_factory() as db:
        await db.execute(delete(Fixture).where(Fixture.id == fixture.id))
        await db.execute(delete(Team).where(Team.league_id == league.id))
        await db.execute(delete(League).where(League.id == league.id))
        await db.execute(delete(Sport).where(Sport.id == sport.id))
        await db.commit()


async def test_fetch_head_to_head_returns_none_for_non_football_sport(
    seeded_fixture_with_external_ids, monkeypatch
):
    called = False

    async def _fake_fetch(*args, **kwargs):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr("app.adapters.api_football.fetch_h2h_detail", _fake_fetch)

    async with async_session_factory() as db:
        result = await _fetch_head_to_head(db, "nba", seeded_fixture_with_external_ids)

    assert result is None
    assert called is False  # never even attempts the live call for a non-football sport


async def test_fetch_head_to_head_returns_none_when_team_missing_external_id(
    seeded_fixture_without_external_ids, monkeypatch
):
    called = False

    async def _fake_fetch(*args, **kwargs):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr("app.adapters.api_football.fetch_h2h_detail", _fake_fetch)

    async with async_session_factory() as db:
        result = await _fetch_head_to_head(db, "football", seeded_fixture_without_external_ids)

    assert result is None
    assert called is False


async def test_fetch_head_to_head_maps_real_detail_for_football(
    seeded_fixture_with_external_ids, monkeypatch
):
    fake_detail = H2HDetail(
        meetings_count=2,
        home_wins=1,
        draws=1,
        away_wins=0,
        avg_goals_home=1.5,
        avg_goals_away=1.0,
        avg_corners_home=6.0,
        avg_corners_away=4.5,
        avg_shots_home=14.0,
        avg_shots_away=10.0,
        avg_shots_on_goal_home=5.0,
        avg_shots_on_goal_away=3.0,
        avg_possession_home=55.0,
        avg_possession_away=45.0,
    )

    captured_ids = []

    async def _fake_fetch(home_external_id, away_external_id):
        captured_ids.append((home_external_id, away_external_id))
        return fake_detail

    monkeypatch.setattr("app.adapters.api_football.fetch_h2h_detail", _fake_fetch)

    async with async_session_factory() as db:
        result = await _fetch_head_to_head(db, "football", seeded_fixture_with_external_ids)

    assert captured_ids == [("100", "200")]
    assert result is not None
    assert result.meetings_count == 2
    assert result.home_wins == 1
    assert result.draws == 1
    assert result.away_wins == 0
    assert result.avg_goals_home == 1.5
    assert result.avg_goals_away == 1.0
    assert result.avg_corners_home == 6.0
    assert result.avg_shots_on_goal_away == 3.0
    assert result.avg_possession_home == 55.0


async def test_fetch_head_to_head_returns_none_when_adapter_has_no_real_meetings(
    seeded_fixture_with_external_ids, monkeypatch
):
    """Real teams that have simply never played each other before — a genuine, honest None,
    not a fabricated empty-but-present record."""

    async def _fake_fetch(home_external_id, away_external_id):
        return None

    monkeypatch.setattr("app.adapters.api_football.fetch_h2h_detail", _fake_fetch)

    async with async_session_factory() as db:
        result = await _fetch_head_to_head(db, "football", seeded_fixture_with_external_ids)

    assert result is None
