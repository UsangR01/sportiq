"""GET /fixtures's combined-best-pick and min_probability/min_odds/market filtering — added so
the merged Home/Picks feed can surface "only fixtures whose best pick, drawn from every market,
clears a probability and odds floor" per the user's explicit request (see CLAUDE.md)."""

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
async def seeded_multi_market_fixtures():
    """Two fixtures: one whose best cross-market pick clears 60% + real odds (corners_total,
    under 9.5 @ high probability), one whose best pick stays below 60% everywhere."""
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

        now = datetime.now(UTC)

        # Fixture A: h2h probabilities are all modest (<60%), but corners "under 9.5" is
        # confidently high (corners_xg sums to a low total) and has real odds.
        fixture_a = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            external_id="fx-combined-a",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=kickoff,
            status=FixtureStatus.SCHEDULED,
            season="2026",
        )
        db.add(fixture_a)
        await db.flush()
        db.add(
            Odds(
                fixture_id=fixture_a.id,
                bookmaker="TestBook",
                market=OddsMarket.CORNERS_TOTAL,
                line=9.5,
                over_odds=3.5,
                under_odds=1.30,
                updated_at=now,
            )
        )
        db.add(
            Prediction(
                fixture_id=fixture_a.id,
                model_version="test_model_v1",
                home_prob=0.40,
                draw_prob=0.30,
                away_prob=0.30,
                confidence_tier=ConfidenceTier.MEDIUM,
                corners_xg_home=2.0,
                corners_xg_away=1.5,  # low total -> confidently "under"
                created_at=now,
            )
        )

        # Fixture B: every market stays a coin-flip or worse — nothing should clear 60%.
        fixture_b = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            external_id="fx-combined-b",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=kickoff + timedelta(hours=1),
            status=FixtureStatus.SCHEDULED,
            season="2026",
        )
        db.add(fixture_b)
        await db.flush()
        db.add(
            Odds(
                fixture_id=fixture_b.id,
                bookmaker="TestBook",
                market=OddsMarket.H2H,
                home_odds=2.5,
                draw_odds=3.2,
                away_odds=3.0,
                updated_at=now,
            )
        )
        db.add(
            Prediction(
                fixture_id=fixture_b.id,
                model_version="test_model_v1",
                home_prob=0.40,
                draw_prob=0.30,
                away_prob=0.30,
                confidence_tier=ConfidenceTier.LOW,
                created_at=now,
            )
        )
        await db.commit()
        await db.refresh(sport)
        await db.refresh(fixture_a)
        await db.refresh(fixture_b)

    yield sport, fixture_a, fixture_b

    async with async_session_factory() as db:
        await db.execute(delete(Odds).where(Odds.fixture_id.in_([fixture_a.id, fixture_b.id])))
        await db.execute(
            delete(Prediction).where(Prediction.fixture_id.in_([fixture_a.id, fixture_b.id]))
        )
        await db.execute(delete(Fixture).where(Fixture.sport_id == sport.id))
        await db.execute(delete(Team).where(Team.sport_id == sport.id))
        await db.execute(delete(League).where(League.sport_id == sport.id))
        await db.execute(delete(Sport).where(Sport.id == sport.id))
        await db.commit()


async def test_min_probability_drops_fixtures_below_threshold(
    api_client, seeded_multi_market_fixtures
):
    sport, fixture_a, fixture_b = seeded_multi_market_fixtures
    response = await api_client.get(
        "/fixtures", params={"sport_slug": sport.slug, "min_probability": 0.6}
    )
    assert response.status_code == 200
    ids = {row["id"] for row in response.json()}
    assert str(fixture_a.id) in ids
    assert str(fixture_b.id) not in ids


async def test_min_probability_best_pick_drawn_from_corners_market(
    api_client, seeded_multi_market_fixtures
):
    sport, fixture_a, _fixture_b = seeded_multi_market_fixtures
    response = await api_client.get(
        "/fixtures", params={"sport_slug": sport.slug, "min_probability": 0.6}
    )
    body = response.json()
    row = next(r for r in body if r["id"] == str(fixture_a.id))
    assert row["best_pick"]["market"] == "corners_total"
    assert row["best_pick"]["selection"] == "under"
    assert row["best_pick"]["line"] == 9.5
    assert row["best_pick"]["odds"] == pytest.approx(1.30)
    assert row["best_pick"]["probability"] >= 0.6


async def test_min_odds_excludes_probability_only_picks(api_client, seeded_multi_market_fixtures):
    """A fixture whose only qualifying-probability pick has NO real odds must be excluded when
    min_odds is set — never fabricate an odds floor against a probability-only pick."""
    sport, fixture_a, _fixture_b = seeded_multi_market_fixtures
    # Push min_odds above fixture_a's real corners odds (1.30) so it should be excluded too.
    response = await api_client.get(
        "/fixtures",
        params={"sport_slug": sport.slug, "min_probability": 0.6, "min_odds": 2.0},
    )
    ids = {row["id"] for row in response.json()}
    assert str(fixture_a.id) not in ids


async def test_market_filter_restricts_to_one_market(api_client, seeded_multi_market_fixtures):
    sport, fixture_a, fixture_b = seeded_multi_market_fixtures
    response = await api_client.get("/fixtures", params={"sport_slug": sport.slug, "market": "h2h"})
    body = response.json()
    row_a = next(r for r in body if r["id"] == str(fixture_a.id))
    # fixture_a has no h2h odds at all, so with market=h2h its best_pick falls back to
    # probability-only among home/draw/away (never corners, since that's excluded now).
    assert row_a["best_pick"]["market"] == "h2h"
    row_b = next(r for r in body if r["id"] == str(fixture_b.id))
    assert row_b["best_pick"]["market"] == "h2h"


async def test_goals_total_market_requires_line(api_client, seeded_multi_market_fixtures):
    sport, _a, _b = seeded_multi_market_fixtures
    response = await api_client.get(
        "/fixtures", params={"sport_slug": sport.slug, "market": "goals_total"}
    )
    assert response.status_code == 422
