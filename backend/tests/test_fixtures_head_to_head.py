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
        # Unique per test. These ids are now the H2H CACHE KEY (h2h:detail:<home>:<away>),
        # so the previous hardcoded "100"/"200" made every test in this file share one entry:
        # whichever ran first populated the cache and the rest silently received its payload
        # instead of their own fake. Tests must not be able to see each other's data.
        home_ext, away_ext = f"h{uuid.uuid4().hex[:8]}", f"a{uuid.uuid4().hex[:8]}"
        home = Team(sport_id=sport.id, league_id=league.id, name="Home FC", external_id=home_ext)
        away = Team(sport_id=sport.id, league_id=league.id, name="Away FC", external_id=away_ext)
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


async def test_a_sport_with_no_h2h_provider_never_calls_footballs(
    seeded_fixture_with_external_ids, monkeypatch
):
    """THIS ASSERTED "nba returns None" UNTIL 2026-08-14, and that is no longer the contract:
    basketball and tennis now have their own panels, through BallDontLie. What must still hold
    is that a sport with no H2H source of its own does not silently fall through to
    API-Football's -- which would ask the football provider for two teams it has never heard
    of and spend real quota doing it."""
    called = False

    async def _fake_fetch(*args, **kwargs):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr("app.adapters.api_football.fetch_h2h_detail", _fake_fetch)

    async with async_session_factory() as db:
        result = await _fetch_head_to_head(db, "nfl", seeded_fixture_with_external_ids)

    assert result is None
    assert called is False


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

    # The seeded ids are random per test (they are the cache key), so assert the adapter was
    # asked for THIS fixture's own teams rather than for two hardcoded literals.
    async with async_session_factory() as db:
        home = await db.get(Team, seeded_fixture_with_external_ids.home_team_id)
        away = await db.get(Team, seeded_fixture_with_external_ids.away_team_id)
    assert captured_ids == [(home.external_id, away.external_id)]
    assert result is not None
    assert result.meetings_count == 2
    assert result.home_wins == 1
    assert result.draws == 1
    assert result.away_wins == 0
    # Named avg_* fields became a generic labelled list when tennis and basketball joined:
    # the three sports do not share a stat vocabulary, so football's five rows would have sat
    # beside eleven permanently-null tennis fields. See HeadToHeadStat.
    by_label = {stat.label: stat for stat in result.stats}
    assert by_label["Goals"].home == 1.5
    assert by_label["Goals"].away == 1.0
    assert by_label["Corners"].home == 6.0
    assert by_label["Possession"].suffix == "%"
    assert by_label["Goals"].suffix == ""
    assert by_label["Shots on goal"].away == 3.0
    assert by_label["Possession"].home == 55.0
    # Every football row survives the mapping -- five, in display order.
    assert [stat.label for stat in result.stats] == [
        "Goals",
        "Corners",
        "Total shots",
        "Shots on goal",
        "Possession",
    ]


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
