"""GET /fixtures/{id}'s prediction.extra_markets — real DB-backed proof that double chance
and Over/Under goals/corners are actually derived from a stored Prediction row's xg_home/away
and corners_xg_home/away, not left as dead schema fields (see app/fixtures/router.py:
_build_extra_markets and app/models_ml/markets.py)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.main import app
from app.predictions.models import ConfidenceTier, Prediction
from app.sports.models import League, Sport


@pytest.fixture
async def api_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def seeded_fixture_with_full_prediction():
    kickoff = datetime.now(UTC) + timedelta(days=1)
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
        home = Team(sport_id=sport.id, league_id=league.id, name="Home FC", external_id="1")
        away = Team(sport_id=sport.id, league_id=league.id, name="Away FC", external_id="2")
        db.add_all([home, away])
        await db.flush()
        fixture = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            external_id="fx-extra-markets-1",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=kickoff,
            status=FixtureStatus.SCHEDULED,
            season="2026",
        )
        db.add(fixture)
        await db.flush()
        db.add(
            Prediction(
                fixture_id=fixture.id,
                model_version="test_model_v1",
                home_prob=0.5,
                draw_prob=0.3,
                away_prob=0.2,
                confidence_tier=ConfidenceTier.HIGH,
                xg_home=1.5,
                xg_away=1.0,
                corners_xg_home=5.0,
                corners_xg_away=4.0,
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()
        await db.refresh(sport)
        await db.refresh(fixture)

    yield sport, fixture

    async with async_session_factory() as db:
        await db.execute(delete(Prediction).where(Prediction.fixture_id == fixture.id))
        await db.execute(delete(Fixture).where(Fixture.sport_id == sport.id))
        await db.execute(delete(Team).where(Team.sport_id == sport.id))
        await db.execute(delete(League).where(League.sport_id == sport.id))
        await db.execute(delete(Sport).where(Sport.id == sport.id))
        await db.commit()


async def test_fixture_detail_includes_double_chance(
    api_client, seeded_fixture_with_full_prediction
):
    _sport, fixture = seeded_fixture_with_full_prediction
    response = await api_client.get(f"/fixtures/{fixture.id}")
    assert response.status_code == 200
    extra = response.json()["prediction"]["extra_markets"]
    assert extra["double_chance_home_or_draw_prob"] == pytest.approx(0.8)  # home+draw
    assert extra["double_chance_away_or_draw_prob"] == pytest.approx(0.5)  # away+draw


async def test_fixture_detail_includes_goals_totals_for_all_supported_lines(
    api_client, seeded_fixture_with_full_prediction
):
    _sport, fixture = seeded_fixture_with_full_prediction
    response = await api_client.get(f"/fixtures/{fixture.id}")
    goals_totals = response.json()["prediction"]["extra_markets"]["goals_totals"]
    lines = {t["line"] for t in goals_totals}
    assert lines == {1.5, 2.5, 3.5}
    for t in goals_totals:
        assert t["under_prob"] + t["over_prob"] == pytest.approx(1.0)


async def test_fixture_detail_includes_corners_totals(
    api_client, seeded_fixture_with_full_prediction
):
    _sport, fixture = seeded_fixture_with_full_prediction
    response = await api_client.get(f"/fixtures/{fixture.id}")
    corners_totals = response.json()["prediction"]["extra_markets"]["corners_totals"]
    assert len(corners_totals) == 1
    assert corners_totals[0]["line"] == 9.5
    assert corners_totals[0]["under_prob"] + corners_totals[0]["over_prob"] == pytest.approx(1.0)


async def test_fixture_detail_suppresses_prediction_once_postponed(
    api_client, seeded_fixture_with_full_prediction
):
    """A fixture postponed after its Prediction row was already written must not keep
    surfacing that pre-postponement call — per explicit user report, the original market
    prediction/percentage/odds should never still be shown once a game isn't being played."""
    sport, fixture = seeded_fixture_with_full_prediction
    async with async_session_factory() as db:
        db_fixture = (
            await db.execute(select(Fixture).where(Fixture.id == fixture.id))
        ).scalar_one()
        db_fixture.status = FixtureStatus.POSTPONED
        await db.commit()

    response = await api_client.get(f"/fixtures/{fixture.id}")
    body = response.json()
    assert body["status"] == "postponed"
    assert body["prediction"] is None
    assert body["best_pick"] is None
