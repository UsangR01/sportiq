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

        # Fixture A: h2h probabilities are all modest (<60%), but corners "under 9.5" clears
        # 60% and has real odds, so it should win best_pick.
        #
        # The corners_xg values below deliberately sum to a REALISTIC 8.0 (P(under 9.5) ≈ 0.72
        # against the 1.30 price, i.e. ~0.77 implied). They previously summed to 3.5, which
        # implies P(under 9.5) ≈ 0.999 — a near-certainty that no real bookmaker would price at
        # 1.30, and a ~23-point disagreement with the market. Once _pick_best gained its
        # edge-vs-market guard (MAX_EDGE_OVER_MARKET) that fixture was correctly rejected as
        # implausible, which is the guard working, not a regression — so the seed was made
        # realistic rather than the guard loosened to accommodate synthetic data.
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
                # 0.55 sits +0.092 above football's real 0.4582 home base rate, so h2h has a
                # genuinely informative candidate for the market=h2h tests. It stays below the
                # +0.10 a 0.6 floor demands, so corners (+0.265) still wins unrestricted.
                home_prob=0.55,
                draw_prob=0.25,
                away_prob=0.20,
                confidence_tier=ConfidenceTier.MEDIUM,
                corners_xg_home=4.5,
                corners_xg_away=3.5,  # total 8.0 -> P(under 9.5) ~= 0.72, a realistic edge
                created_at=now,
            )
        )

        # Fixture B: every market stays a coin-flip or worse — nothing clears 60%, so the
        # fixture should be dropped entirely by a 60% floor.
        #
        # 0.45/0.10/0.45 is deliberate, for the same reason as the completed-fixture seed
        # below: the previous 0.40/0.30/0.30 put double chance 1X at 0.70, well clear of the
        # floor this fixture exists to fail. It only passed because the old _pick_best always
        # preferred a PRICED candidate (h2h, all < 0.6) over an unpriced one, so the 0.70 1X
        # was never considered at all. Now that the floor is applied before ranking, that
        # unpriced 1X would legitimately surface — correct behaviour, but it made this
        # fixture stop testing what it claims. These values leave 1X and X2 both at 0.55.
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
                # away 0.38 is +0.092 over the 0.2879 away base rate: informative enough to
                # surface when no floor is set, but short of the +0.10 a 0.6 floor requires,
                # so this fixture is still correctly dropped by that floor.
                home_prob=0.42,
                draw_prob=0.20,
                away_prob=0.38,
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


async def test_min_odds_excludes_a_pick_whose_real_odds_are_below_the_floor(
    api_client, seeded_multi_market_fixtures
):
    """A pick with a REAL price below the floor is never the one shown — the odds slider must
    keep working wherever odds actually exist.

    WHAT CHANGED 2026-08-23: the floor now filters CANDIDATES rather than the winner, so the
    fixture is no longer dropped outright — a different candidate takes over. Previously the
    top-probability pick was chosen first and then rejected, which deleted the whole card even
    when another candidate qualified. Measured on 45 upcoming fixtures, at min_odds 1.2 eleven
    cards vanished and ten of them had a >=70% alternative clearing the floor.

    What must still hold is the part users can see: the 1.30 corners pick is not what surfaces.
    """
    sport, fixture_a, _fixture_b = seeded_multi_market_fixtures
    response = await api_client.get(
        "/fixtures",
        params={"sport_slug": sport.slug, "min_probability": 0.6, "min_odds": 2.0},
    )
    row = next((r for r in response.json() if r["id"] == str(fixture_a.id)), None)
    if row is not None:
        pick = row["best_pick"]
        # Either no price at all (the documented tennis exemption) or one clearing the floor.
        assert pick["odds"] is None or pick["odds"] >= 2.0
        assert not (pick["market"] == "corners_total" and pick["odds"] == pytest.approx(1.30))


async def test_a_below_floor_pick_falls_back_instead_of_deleting_the_card(
    api_client, seeded_multi_market_fixtures
):
    """THE POINT OF THE CHANGE. min_probability had always filtered candidates BEFORE the pick
    was chosen; min_odds was applied afterwards to the winner alone, so a fixture whose
    top-probability pick priced at 1.06 disappeared entirely even with a 74% candidate at 1.48
    sitting behind it. Both floors now select among candidates."""
    sport, fixture_a, _fixture_b = seeded_multi_market_fixtures

    low = await api_client.get(
        "/fixtures", params={"sport_slug": sport.slug, "min_probability": 0.6, "min_odds": 1.01}
    )
    high = await api_client.get(
        "/fixtures", params={"sport_slug": sport.slug, "min_probability": 0.6, "min_odds": 2.0}
    )
    shown_low = next((r for r in low.json() if r["id"] == str(fixture_a.id)), None)
    shown_high = next((r for r in high.json() if r["id"] == str(fixture_a.id)), None)

    assert shown_low is not None, "the fixture is visible at the default floor"
    # Raising the floor may change the pick, but must not silently show the rejected price.
    if shown_high is not None:
        odds = shown_high["best_pick"]["odds"]
        assert odds is None or odds >= 2.0


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
    """A COMPLETED fixture whose best pick genuinely never clears 60% in ANY market.

    The probabilities below are deliberately 0.45/0.10/0.45: both double-chance combinations
    (1X = home+draw, X2 = away+draw) land at 0.55, and no xg_* values are set so there are no
    Over/Under candidates either. An earlier version used 0.40/0.30/0.30, which looked
    low-confidence but actually produced a 1X pick at 0.70 — so the test that depended on it
    was passing for the wrong reason from the moment combined-market picks were introduced."""
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
                # Tuned so ONE fixture exercises both halves of the contract. X2 lands at
                # 0.595: above the 0.5 floor and +0.053 over its 0.5418 base rate, so a 0.5
                # floor keeps it; below the 0.6 floor, so a 0.6 floor drops it. 1X lands at
                # 0.60 - it would clear a 0.6 floor on probability alone, but sits 0.112 BELOW
                # its own 0.7121 base rate, so the informativeness bar correctly refuses it.
                home_prob=0.405,
                draw_prob=0.195,
                away_prob=0.40,
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


async def test_min_probability_applies_to_completed_fixtures_too(
    api_client, seeded_completed_low_confidence_fixture
):
    """Per explicit user request ("the probability and odd slider does not apply to the tennis
    records. Make it apply"): a COMPLETED fixture whose best pick is below the probability
    floor is now hidden, so past-performance review is scoped to the picks the user would
    actually have taken at their own confidence bar.

    This deliberately narrows the earlier blanket "completed fixtures bypass every floor"
    exemption (added for "Results of past games are missing") rather than removing it — a
    completed fixture that DOES clear the bar is still shown, as the next test proves."""
    sport, fixture = seeded_completed_low_confidence_fixture
    response = await api_client.get(
        "/fixtures",
        params={"sport_slug": sport.slug, "min_probability": 0.6},
    )
    assert response.status_code == 200
    ids = {row["id"] for row in response.json()}
    assert str(fixture.id) not in ids


async def test_completed_fixtures_clearing_the_floor_are_still_shown(
    api_client, seeded_completed_low_confidence_fixture
):
    """The other half of the contract above — past results don't vanish wholesale, which was
    the original "Results of past games are missing" complaint. Same fixture, a floor its best
    pick (0.55 double chance) does clear."""
    sport, fixture = seeded_completed_low_confidence_fixture
    response = await api_client.get(
        "/fixtures",
        params={"sport_slug": sport.slug, "min_probability": 0.5},
    )
    ids = {row["id"] for row in response.json()}
    assert str(fixture.id) in ids


async def test_min_odds_does_not_hide_a_pick_that_has_no_odds_at_all(
    api_client, seeded_completed_low_confidence_fixture
):
    """An odds floor is unanswerable for a sport with no odds coverage yet (tennis —
    BallDontLie's tennis /odds is GOAT-tier gated). The previous "no odds -> fails the floor"
    rule made every upcoming tennis fixture silently invisible the moment the odds slider moved
    off its minimum. This fixture has a real prediction but no Odds rows at all, so it must
    survive an odds floor rather than being filtered on a price we don't have."""
    sport, fixture = seeded_completed_low_confidence_fixture
    response = await api_client.get(
        "/fixtures",
        params={"sport_slug": sport.slug, "min_probability": 0.5, "min_odds": 1.5},
    )
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


def test_pick_best_ranks_by_highest_probability_among_informative_picks():
    """Highest probability wins — but only among picks that actually say something.

    This is the Shenyang Urban 3-1 Shanghai Shenhua case. "Under 3.5 at 68%" looks like the
    confident choice and would win on raw probability, yet it sits BELOW its own 69.4% base
    rate, so it carries no information about this fixture at all. The 57% home call sits well
    above football's 0.4582 home base rate, and it was the correct call."""
    from app.fixtures.router import _MarketCandidate, _pick_best

    uninformative = _MarketCandidate("under", 0.68, 1.68, "goals_total", 3.5)  # below base rate
    informative = _MarketCandidate("home", 0.57, 3.90, "h2h", None)  # +0.11 over base rate

    best = _pick_best([uninformative, informative])
    assert best.market == "h2h"
    assert best.selection == "home"


def test_pick_best_excludes_picks_at_or_below_their_market_base_rate():
    """A pick at its base rate is the league average with a percentage sign on it. With nothing
    else available the fixture correctly yields no pick, rather than a confident-looking one
    that says nothing."""
    from app.fixtures.router import _MarketCandidate, _pick_best

    assert _pick_best([_MarketCandidate("under", 0.68, 1.68, "goals_total", 3.5)]) is None
    assert _pick_best([_MarketCandidate("1X", 0.70, 1.40, "double_chance", None)]) is None


def test_pick_best_guard_only_rejects_an_absurd_disagreement_with_the_market():
    """The edge guard is a sanity backstop now, not an active filter.

    At its old 0.15 bound it rejected Qingdao's 85% under-3.5 purely for beating the price by
    20 points - and that pick won. The base-rate gate removes uninformative picks far more
    precisely than penalising confident ones, so only a genuinely absurd gap (more likely stale
    odds or a broken feature vector than a view) is filtered here."""
    from app.fixtures.router import _MarketCandidate, _pick_best

    # 0.85 vs a 1.53 price (implied 0.65) is a real disagreement, but a legitimate one.
    plausible = _MarketCandidate("under", 0.85, 1.53, "goals_total", 3.5)
    assert _pick_best([plausible]).selection == "under"

    # 0.95 against a 5.00 price (implied 0.20) is not a view, it is broken input.
    absurd = _MarketCandidate("under", 0.95, 5.00, "goals_total", 3.5)
    unpriced_alternative = _MarketCandidate("1X", 0.80, None, "double_chance", None)
    assert _pick_best([absurd, unpriced_alternative]).market == "double_chance"
    assert _pick_best([absurd]) is None


def test_min_probability_filters_on_the_probability_actually_shown():
    """The floor must mean what the UI label says.

    An earlier version made this drive the informativeness requirement instead, which produced
    an indefensible result: at a "75%" setting a pick displaying 85% was hidden (only +0.156
    over its 0.694 base rate) while one displaying 80% was kept (+0.258 over its 0.542 base
    rate). A control labelled "minimum probability" has to filter on the number beside it."""
    from app.fixtures.router import _MarketCandidate, _pick_best

    # Both are informative; they differ only in displayed probability.
    lower = _MarketCandidate("under", 0.85, 1.53, "goals_total", 3.5)
    higher = _MarketCandidate("X2", 0.88, 2.04, "double_chance", None)

    assert _pick_best([lower, higher], min_probability=0.75).probability == 0.88
    assert _pick_best([lower], min_probability=0.75).probability == 0.85
    assert _pick_best([lower], min_probability=0.90) is None


def test_informativeness_is_a_fixed_bar_not_something_the_slider_moves():
    """Raising the floor must never ADMIT a pick that a lower floor excluded. The base-rate
    gate is a fixed quality bar; the slider only ever tightens the probability requirement."""
    from app.fixtures.router import _MarketCandidate, _pick_best

    # 0.68 sits below its own 0.694 base rate — uninformative at every slider position.
    uninformative = _MarketCandidate("under", 0.68, 1.68, "goals_total", 3.5)
    for floor in (0.5, 0.6, 0.65):
        assert _pick_best([uninformative], min_probability=floor) is None


# --- base rates are per SPORT -------------------------------------------------------------


def test_tennis_is_not_judged_against_footballs_home_advantage_nor_against_its_own_tiebreak():
    """Football's base rates must not govern tennis — still true, now satisfied differently.

    THIS TEST PREVIOUSLY ASSERTED THE OPPOSITE OUTCOME, and is rewritten rather than deleted
    because the reasoning it encoded is the thing that was wrong. It argued that a tennis
    "home 55%" call says less than nothing, being 7 points below the 62.17% the lower-id
    tiebreak gives you on its own. The 62.17% is real. What does not follow is treating it as a
    bar a pick must clear:

      - the id ordering is not a strategy anyone can run — a user cannot see our row ordering;
      - it is a proxy for player strength (the lower-id player is the higher-ranked one 69% of
        the time), and rank_diff, the model's primary feature, already prices that in — so the
        gate charged the same fact twice;
      - two base rates summing to 1 put the bars at 0.672 and 0.428, so against a model whose
        probabilities cluster in 0.44-0.69 the AWAY slot almost always cleared and the home slot
        almost never did. Measured over 669 real tennis predictions, 167 (25.0%) came out
        inverted: the product recommended the player the model rated lower.

    Tennis now abstains from the gate entirely, as it already did for markets it does not have.
    Football's home advantage is a real causal effect and keeps its rate.
    """
    from app.fixtures.router import _MarketCandidate, _pick_best

    mediocre = _MarketCandidate("home", 0.55, 1.90, "h2h", None)

    # Football is unchanged: 0.55 - 0.4582 = +0.092 >= 0.05.
    assert _pick_best([mediocre], sport_slug="football") is not None
    # Tennis no longer rejects its own favourite on account of the id ordering.
    best = _pick_best([mediocre], sport_slug="tennis")
    assert best is not None and best.selection == "home"


def test_a_strong_tennis_call_still_surfaces():
    """Unchanged, and worth keeping: removing the gate must not have broken ordinary selection."""
    from app.fixtures.router import _MarketCandidate, _pick_best

    strong = _MarketCandidate("home", 0.85, 1.40, "h2h", None)
    best = _pick_best([strong], sport_slug="tennis")
    assert best is not None and best.selection == "home"


def test_the_tennis_favourite_wins_over_its_own_complement():
    """THE inversion, as a behavioural test rather than a constants check.

    Brandon Nakashima v Rafael Jodar, 2026-08-12: home 0.5616, away 0.4384, result HOME_WIN.
    The old gate rejected home (needed 0.6717) and admitted away (needed 0.4283), so the only
    offerable pick was the side the model rated lower — which lost.
    """
    from app.fixtures.router import _MarketCandidate, _pick_best

    home = _MarketCandidate("home", 0.5616, 1.70, "h2h", None)
    away = _MarketCandidate("away", 0.4384, 2.10, "h2h", None)
    best = _pick_best([home, away], sport_slug="tennis")
    assert best is not None
    assert best.selection == "home", "the model's favourite must not be filtered out from under it"


def test_base_rate_abstains_for_a_market_the_sport_does_not_have():
    """Tennis has no goals market. The tennis table omits those keys rather than zeroing them,
    so the gate must abstain (return None, i.e. no opinion) instead of judging the pick against
    a football number that has no meaning here."""
    from app.fixtures.router import _base_rate, _MarketCandidate

    goals = _MarketCandidate("under", 0.68, 1.60, "goals_total", 3.5)
    assert _base_rate(goals, "tennis") is None
    assert _base_rate(goals, "football") == 0.6941
    # Unknown sport falls back to the football-measured table rather than abstaining, which
    # keeps every existing caller's behaviour unchanged.
    assert _base_rate(goals, None) == 0.6941


def test_football_base_rates_are_unchanged_by_the_per_sport_split():
    """A pure regression guard: the football numbers this gate was calibrated on must not have
    moved when the tennis table was added."""
    from app.fixtures.router import MARKET_BASE_RATES

    assert MARKET_BASE_RATES[("h2h", "home", None)] == 0.4582
    assert MARKET_BASE_RATES[("h2h", "away", None)] == 0.2879
    assert MARKET_BASE_RATES[("goals_total", "under", 3.5)] == 0.6941
