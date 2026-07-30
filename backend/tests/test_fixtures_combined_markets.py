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


async def test_all_market_picks_includes_every_market_not_just_the_best(
    api_client, seeded_multi_market_fixtures
):
    """Per explicit user request: past predictions must show every market (h2h, double
    chance, goals/corners O/U), not just the single winning best_pick."""
    sport, fixture_a, _fixture_b = seeded_multi_market_fixtures
    response = await api_client.get("/fixtures", params={"sport_slug": sport.slug})
    body = response.json()
    row = next(r for r in body if r["id"] == str(fixture_a.id))

    markets = {p["market"] for p in row["all_market_picks"]}
    assert markets == {"h2h", "double_chance", "corners_total"}  # no goals_total: no xg set

    h2h_selections = {p["selection"] for p in row["all_market_picks"] if p["market"] == "h2h"}
    assert h2h_selections == {"home", "draw", "away"}  # every h2h outcome, not just the best

    dc_selections = {
        p["selection"] for p in row["all_market_picks"] if p["market"] == "double_chance"
    }
    assert dc_selections == {"1X", "X2"}


async def test_all_market_picks_ignores_market_query_param(
    api_client, seeded_multi_market_fixtures
):
    """market=h2h restricts best_pick, but all_market_picks always stays the FULL breakdown —
    it's meant for evaluating past performance across every market, independent of whichever
    single market the caller asked best_pick to be restricted to."""
    sport, fixture_a, _fixture_b = seeded_multi_market_fixtures
    response = await api_client.get("/fixtures", params={"sport_slug": sport.slug, "market": "h2h"})
    body = response.json()
    row = next(r for r in body if r["id"] == str(fixture_a.id))
    assert row["best_pick"]["market"] == "h2h"
    markets = {p["market"] for p in row["all_market_picks"]}
    assert "corners_total" in markets  # still present despite market=h2h restricting best_pick


@pytest.fixture
async def seeded_completed_low_confidence_fixture():
    """A COMPLETED fixture whose best pick never clears 60% anywhere — used to prove
    min_probability/min_odds don't hide past results (they only gate future/live picks)."""
    kickoff = datetime.now(UTC) - timedelta(days=1)
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
            external_id="fx-completed-low-conf",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=kickoff,
            status=FixtureStatus.COMPLETED,
            season="2026",
        )
        db.add(fixture)
        await db.flush()
        db.add(
            Prediction(
                fixture_id=fixture.id,
                model_version="test_model_v1",
                home_prob=0.40,
                draw_prob=0.30,
                away_prob=0.30,
                confidence_tier=ConfidenceTier.LOW,
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


async def test_completed_fixtures_are_never_hidden_by_min_probability_or_min_odds(
    api_client, seeded_completed_low_confidence_fixture
):
    """Per explicit user report: "Results of past games are missing" — min_probability/
    min_odds encode "is this worth betting on", which doesn't apply to a finished game being
    reviewed for how the model actually performed."""
    sport, fixture = seeded_completed_low_confidence_fixture
    response = await api_client.get(
        "/fixtures",
        params={"sport_slug": sport.slug, "min_probability": 0.6, "min_odds": 1.5},
    )
    assert response.status_code == 200
    ids = {row["id"] for row in response.json()}
    assert str(fixture.id) in ids


@pytest.fixture
async def seeded_postponed_fixture_with_high_confidence_prediction():
    """A POSTPONED fixture that still carries a real Prediction/Odds pair from before the
    postponement was known — its best pick would easily clear a 60%/1.50 floor if it were
    treated as an ordinary scheduled fixture. Used to prove min_probability/min_odds never
    hide it (same reasoning as a completed fixture) AND that its best_pick/all_market_picks
    are suppressed rather than showing a stale pre-postponement pick as if still live."""
    kickoff = datetime.now(UTC) - timedelta(days=1)
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
            external_id="fx-postponed",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=kickoff,
            status=FixtureStatus.POSTPONED,
            season="2026",
        )
        db.add(fixture)
        await db.flush()
        db.add(
            Odds(
                fixture_id=fixture.id,
                bookmaker="TestBook",
                market=OddsMarket.H2H,
                home_odds=1.30,
                draw_odds=5.0,
                away_odds=8.0,
                updated_at=datetime.now(UTC),
            )
        )
        db.add(
            Prediction(
                fixture_id=fixture.id,
                model_version="test_model_v1",
                home_prob=0.85,
                draw_prob=0.10,
                away_prob=0.05,
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


async def test_postponed_fixtures_are_never_hidden_by_min_probability_or_min_odds(
    api_client, seeded_postponed_fixture_with_high_confidence_prediction
):
    sport, fixture = seeded_postponed_fixture_with_high_confidence_prediction
    response = await api_client.get(
        "/fixtures",
        params={"sport_slug": sport.slug, "min_probability": 0.6, "min_odds": 1.5},
    )
    assert response.status_code == 200
    ids = {row["id"] for row in response.json()}
    assert str(fixture.id) in ids


async def test_postponed_fixtures_never_show_a_stale_pick(
    api_client, seeded_postponed_fixture_with_high_confidence_prediction
):
    """Per explicit user report: a postponed fixture was still showing its original
    market prediction/percentage/odds as if the game were still on. best_pick and
    all_market_picks must both be suppressed regardless of how confident/well-priced the
    pre-postponement Prediction/Odds rows were."""
    sport, fixture = seeded_postponed_fixture_with_high_confidence_prediction
    response = await api_client.get("/fixtures", params={"sport_slug": sport.slug})
    row = next(r for r in response.json() if r["id"] == str(fixture.id))
    assert row["status"] == "postponed"
    assert row["best_pick"] is None
    assert row["all_market_picks"] == []
