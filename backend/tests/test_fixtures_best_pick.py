"""GET /fixtures's best_pick field (the inline selection/probability/odds badge the mobile
Home screen groups by league and highlights above a probability threshold) — seeds real
Sport/League/Team/Fixture/Odds/Prediction rows rather than mocking, matching this project's
DB-touching test convention (see test_model_stats.py)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.main import app
from app.odds.models import Odds
from app.predictions.models import ConfidenceTier, Prediction
from app.sports.models import League, Sport


@pytest.fixture
async def api_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def seeded_fixture_with_odds_and_prediction():
    kickoff = datetime.now(UTC) + timedelta(days=1)

    async with async_session_factory() as db:
        slug = f"test-sport-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Test Sport", model_type="test", active=True)
        db.add(sport)
        await db.flush()

        league = League(
            sport_id=sport.id,
            slug="test-league",
            name="Test Série",
            country="Testland",
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
            external_id="fx-1",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=kickoff,
            status=FixtureStatus.SCHEDULED,
            season="2026",
        )
        db.add(fixture)
        await db.flush()

        db.add(
            Odds(
                fixture_id=fixture.id,
                bookmaker="TestBook",
                market="h2h",
                home_odds=1.50,
                draw_odds=4.00,
                away_odds=6.00,
                updated_at=datetime.now(UTC),
            )
        )
        db.add(
            Prediction(
                fixture_id=fixture.id,
                model_version="test_model_v1",
                home_prob=0.70,
                draw_prob=0.20,
                away_prob=0.10,
                confidence_tier=ConfidenceTier.HIGH,
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()
        await db.refresh(sport)
        await db.refresh(fixture)

    yield sport, fixture

    async with async_session_factory() as db:
        await db.execute(delete(Odds).where(Odds.fixture_id == fixture.id))
        await db.execute(delete(Prediction).where(Prediction.fixture_id == fixture.id))
        await db.execute(delete(Fixture).where(Fixture.sport_id == sport.id))
        await db.execute(delete(Team).where(Team.sport_id == sport.id))
        await db.execute(delete(League).where(League.sport_id == sport.id))
        await db.execute(delete(Sport).where(Sport.id == sport.id))
        await db.commit()


async def test_list_fixtures_includes_league_name_and_country(
    api_client, seeded_fixture_with_odds_and_prediction
):
    sport, _fixture = seeded_fixture_with_odds_and_prediction
    response = await api_client.get("/fixtures", params={"sport_slug": sport.slug})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["league_name"] == "Test Série"
    assert body[0]["league_country"] == "Testland"


async def test_list_fixtures_best_pick_matches_highest_probability_with_real_odds(
    api_client, seeded_fixture_with_odds_and_prediction
):
    sport, _fixture = seeded_fixture_with_odds_and_prediction
    response = await api_client.get("/fixtures", params={"sport_slug": sport.slug})
    body = response.json()

    best_pick = body[0]["best_pick"]
    assert best_pick is not None
    assert best_pick["selection"] == "home"  # highest probability (0.70) AND has real odds
    assert best_pick["probability"] == pytest.approx(0.70)
    assert best_pick["odds"] == pytest.approx(1.50)


async def test_list_fixtures_best_pick_falls_back_to_probability_when_no_odds(api_client):
    """A prediction with zero real odds still gets a best_pick (probability-only, odds=None)
    — /fixtures never filters a fixture out for lacking odds the way /picks does; the mobile
    client decides how prominently to show it. best_pick is drawn from ACROSS every market
    (see app/fixtures/router.py:_all_market_candidates), so with home=0.20/draw=0.25/away=0.55,
    the double-chance X2 (away+draw=0.80) beats every single-outcome h2h candidate — this is
    the intended "highest probability of winning across all markets" behavior, not a bug."""
    kickoff = datetime.now(UTC) + timedelta(days=1)
    async with async_session_factory() as db:
        slug = f"test-sport-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Test Sport", model_type="test", active=True)
        db.add(sport)
        await db.flush()
        league = League(
            sport_id=sport.id,
            slug="test-league",
            name="No Odds League",
            country=None,
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
            external_id="fx-2",
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
                home_prob=0.20,
                draw_prob=0.25,
                away_prob=0.55,
                confidence_tier=ConfidenceTier.MEDIUM,
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()
        await db.refresh(sport)

    try:
        response = await api_client.get("/fixtures", params={"sport_slug": sport.slug})
        body = response.json()
        best_pick = body[0]["best_pick"]
        assert best_pick is not None
        assert best_pick["selection"] == "X2"
        assert best_pick["market"] == "double_chance"
        assert best_pick["probability"] == pytest.approx(0.80)
        assert best_pick["odds"] is None
        assert body[0]["league_country"] is None
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Prediction).where(Prediction.model_version == "test_model_v1"))
            await db.execute(delete(Fixture).where(Fixture.sport_id == sport.id))
            await db.execute(delete(Team).where(Team.sport_id == sport.id))
            await db.execute(delete(League).where(League.sport_id == sport.id))
            await db.execute(delete(Sport).where(Sport.id == sport.id))
            await db.commit()
