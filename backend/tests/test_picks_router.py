"""GET /picks — real DB-backed regression test for a bug where a fixture appeared in /picks
results even though its actually-recommended selection's own odds were below min_odds (the
threshold check used to test the MAX odds across all three markets, not the odds of the
specific outcome being shown — see CLAUDE.md). Also covers the newer double_chance/goals_total/
corners_total markets (same endpoint, generalised via `market`/`line` query params)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.main import app
from app.odds.models import Odds, OddsMarket
from app.predictions.models import ConfidenceTier, Prediction
from app.sports.models import League, Sport


@pytest.fixture
async def api_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def seeded_fixture(request):
    """home_odds/draw_odds/away_odds/probs are parametrized per-test via request.param."""
    kickoff = datetime.now(UTC) + timedelta(days=1)
    params = request.param

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
            external_id="fx-picks-1",
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
                home_odds=params["home_odds"],
                draw_odds=params["draw_odds"],
                away_odds=params["away_odds"],
                updated_at=datetime.now(UTC),
            )
        )
        db.add(
            Prediction(
                fixture_id=fixture.id,
                model_version="test_model_v1",
                home_prob=params["home_prob"],
                draw_prob=params["draw_prob"],
                away_prob=params["away_prob"],
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


@pytest.mark.parametrize(
    "seeded_fixture",
    [
        {
            # The model favours home (0.70) at odds 1.30 — below the 2.0 threshold below.
            # away_odds=6.0 clears the threshold, but away is NOT the recommended selection.
            "home_prob": 0.70,
            "draw_prob": 0.20,
            "away_prob": 0.10,
            "home_odds": 1.30,
            "draw_odds": 4.00,
            "away_odds": 6.00,
        }
    ],
    indirect=True,
)
async def test_picks_excludes_fixture_when_recommended_selection_is_below_threshold(
    api_client, seeded_fixture
):
    sport, _fixture = seeded_fixture
    response = await api_client.get("/picks", params={"min_odds": 2.0, "sport_slug": sport.slug})
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize(
    "seeded_fixture",
    [
        {
            "home_prob": 0.70,
            "draw_prob": 0.20,
            "away_prob": 0.10,
            "home_odds": 2.50,
            "draw_odds": 4.00,
            "away_odds": 6.00,
        }
    ],
    indirect=True,
)
async def test_picks_includes_fixture_when_recommended_selection_meets_threshold(
    api_client, seeded_fixture
):
    sport, fixture = seeded_fixture
    response = await api_client.get("/picks", params={"min_odds": 2.0, "sport_slug": sport.slug})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["fixture_id"] == str(fixture.id)
    assert body[0]["selection"] == "home"
    assert body[0]["odds"] == pytest.approx(2.50)


@pytest.fixture
async def seeded_multi_market_fixture():
    """One fixture with odds across all four markets, plus xg_home/away and corners_xg_home/
    away on its Prediction — enough to exercise every market /picks now supports."""
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
            external_id="fx-multimarket-1",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=kickoff,
            status=FixtureStatus.SCHEDULED,
            season="2026",
        )
        db.add(fixture)
        await db.flush()

        now = datetime.now(UTC)
        db.add_all(
            [
                Odds(
                    fixture_id=fixture.id,
                    bookmaker="TestBook",
                    market=OddsMarket.H2H,
                    home_odds=1.90,
                    draw_odds=3.60,
                    away_odds=4.20,
                    updated_at=now,
                ),
                Odds(
                    fixture_id=fixture.id,
                    bookmaker="TestBook",
                    market=OddsMarket.DOUBLE_CHANCE,
                    home_odds=1.20,
                    away_odds=1.60,
                    updated_at=now,  # 1X, X2
                ),
                Odds(
                    fixture_id=fixture.id,
                    bookmaker="TestBook",
                    market=OddsMarket.TOTAL,
                    line=2.5,
                    over_odds=2.00,
                    under_odds=1.80,
                    updated_at=now,
                ),
                Odds(
                    fixture_id=fixture.id,
                    bookmaker="TestBook",
                    market=OddsMarket.CORNERS_TOTAL,
                    line=9.5,
                    over_odds=1.85,
                    under_odds=1.90,
                    updated_at=now,
                ),
            ]
        )
        db.add(
            Prediction(
                fixture_id=fixture.id,
                model_version="test_model_v1",
                home_prob=0.55,
                draw_prob=0.25,
                away_prob=0.20,
                confidence_tier=ConfidenceTier.HIGH,
                xg_home=1.8,
                xg_away=0.9,
                corners_xg_home=5.5,
                corners_xg_away=4.8,
                created_at=now,
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


async def test_picks_double_chance_market(api_client, seeded_multi_market_fixture):
    sport, fixture = seeded_multi_market_fixture
    response = await api_client.get(
        "/picks", params={"min_odds": 1.01, "sport_slug": sport.slug, "market": "double_chance"}
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    # 1X = home(0.55)+draw(0.25) = 0.80 @ 1.20; X2 = away(0.20)+draw(0.25) = 0.45 @ 1.60.
    # 1X has the higher model probability, so it's the recommended selection.
    assert body[0]["fixture_id"] == str(fixture.id)
    assert body[0]["market"] == "double_chance"
    assert body[0]["selection"] == "1X"
    assert body[0]["odds"] == pytest.approx(1.20)
    assert body[0]["model_probability"] == pytest.approx(0.80)


async def test_picks_goals_total_market(api_client, seeded_multi_market_fixture):
    sport, fixture = seeded_multi_market_fixture
    response = await api_client.get(
        "/picks",
        params={
            "min_odds": 1.01,
            "sport_slug": sport.slug,
            "market": "goals_total",
            "line": 2.5,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["fixture_id"] == str(fixture.id)
    assert body[0]["market"] == "goals_total"
    assert body[0]["line"] == 2.5
    assert body[0]["selection"] in ("over", "under")


async def test_picks_corners_total_market(api_client, seeded_multi_market_fixture):
    sport, fixture = seeded_multi_market_fixture
    response = await api_client.get(
        "/picks",
        params={
            "min_odds": 1.01,
            "sport_slug": sport.slug,
            "market": "corners_total",
            "line": 9.5,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["fixture_id"] == str(fixture.id)
    assert body[0]["market"] == "corners_total"
    assert body[0]["line"] == 9.5


async def test_picks_goals_total_requires_line(api_client, seeded_multi_market_fixture):
    sport, _fixture = seeded_multi_market_fixture
    response = await api_client.get(
        "/picks", params={"min_odds": 1.01, "sport_slug": sport.slug, "market": "goals_total"}
    )
    assert response.status_code == 422


async def test_picks_goals_total_rejects_unsupported_line(api_client, seeded_multi_market_fixture):
    sport, _fixture = seeded_multi_market_fixture
    response = await api_client.get(
        "/picks",
        params={
            "min_odds": 1.01,
            "sport_slug": sport.slug,
            "market": "goals_total",
            "line": 5.5,
        },
    )
    assert response.status_code == 422


async def test_picks_h2h_rejects_line_param(api_client, seeded_multi_market_fixture):
    sport, _fixture = seeded_multi_market_fixture
    response = await api_client.get(
        "/picks", params={"min_odds": 1.01, "sport_slug": sport.slug, "market": "h2h", "line": 2.5}
    )
    assert response.status_code == 422
