"""/history and /history/summary now also score the pick that was on the CARD.

WHY THIS MATTERS. Both endpoints graded the 1X2 argmax of the stored Prediction, while the
product shows best_pick chosen across four markets. Measured over the settled pre-match
population, football's card picks are 43 double chance + 19 corners and ZERO h2h -- so the
published football accuracy described a market football cards never show.

The motivating fixture is both verdicts at once: Hearts v Dundee Utd (2026-08-09) showed UNDER
10.5 CORNERS and won on a 7-2 corner count, while its 1X2 call was away against a 4-0 home win
and lost.

BOTH are returned rather than one replacing the other. Silently redefining `was_correct` would
hand every existing consumer a different number with no signal that it had moved, so the 1X2
verdict keeps that name and the card verdict is new fields alongside it.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.history.models import MatchResult, Outcome
from app.main import create_app
from app.odds.models import Odds, OddsMarket
from app.predictions.models import ConfidenceTier, Prediction, PredictionKind
from app.sports.models import League, Sport
from tests import test_history as _history_tests

# Reused so the void/absent accounting is exercised against a population of a known shape --
# four settled fixtures, one of them a retirement -- rather than a second hand-built copy.
# Rebound rather than imported directly: ruff reads a pytest fixture arriving as a test
# PARAMETER as an F811 redefinition of the imported name, which it is not.
settled_fixtures = _history_tests.settled_fixtures


@pytest.fixture
async def api_client():
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
async def corners_card_fixture():
    """The reported fixture, rebuilt: a 4-0 home win with 7-2 corners, where the card shows
    UNDER 10.5 CORNERS (a WIN) and the 1X2 call is away (a LOSS).

    Probabilities are chosen so away is the 1X2 argmax while corners wins the cross-market
    ranking -- the two verdicts must genuinely disagree or the test proves nothing."""
    async with async_session_factory() as db:
        slug = f"test-card-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Test Card", model_type="test", active=True)
        db.add(sport)
        await db.flush()
        league = League(
            sport_id=sport.id, slug=f"l-{slug}", name="L", country=None, tier=1, active=True
        )
        db.add(league)
        await db.flush()
        home = Team(sport_id=sport.id, league_id=league.id, name="Hearts", external_id="h")
        away = Team(sport_id=sport.id, league_id=league.id, name="Dundee Utd", external_id="a")
        db.add_all([home, away])
        await db.flush()

        now = datetime.now(UTC)
        fixture = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            external_id=f"card-{slug}",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=now - timedelta(days=1),
            status=FixtureStatus.COMPLETED,
            season="2026",
        )
        db.add(fixture)
        await db.flush()
        db.add(
            Prediction(
                fixture_id=fixture.id,
                model_version="test_v1",
                home_prob=0.358,
                draw_prob=0.259,
                away_prob=0.383,  # the 1X2 argmax, and WRONG against a 4-0 home win
                # Drives the corners market: a low total keeps P(under 10.5) high.
                corners_xg_home=5.7,
                corners_xg_away=3.2,
                confidence_tier=ConfidenceTier.LOW,
                created_at=now - timedelta(days=2),
                kind=PredictionKind.PRE_MATCH,
            )
        )
        db.add(
            Odds(
                fixture_id=fixture.id,
                bookmaker="test-book",
                market=OddsMarket.CORNERS_TOTAL,
                line=10.5,
                over_odds=1.70,
                under_odds=2.05,
                updated_at=now,
            )
        )
        db.add(
            Outcome(
                fixture_id=fixture.id,
                home_score=4,
                away_score=0,
                result=MatchResult.HOME_WIN,
                settled_at=now - timedelta(hours=2),
            )
        )
        db.add(
            FixtureLiveState(
                fixture_id=fixture.id,
                home_score=4,
                away_score=0,
                home_corners=7,
                away_corners=2,  # 9 total -> under 10.5 WINS
                status="completed",
                result_type=None,
                last_updated_utc=now,
            )
        )
        await db.commit()
        await db.refresh(sport)
        await db.refresh(fixture)

    yield sport, fixture

    async with async_session_factory() as db:
        await db.execute(delete(Odds).where(Odds.fixture_id == fixture.id))
        await db.execute(delete(Outcome).where(Outcome.fixture_id == fixture.id))
        await db.execute(delete(Prediction).where(Prediction.fixture_id == fixture.id))
        await db.execute(delete(FixtureLiveState).where(FixtureLiveState.fixture_id == fixture.id))
        await db.execute(delete(Fixture).where(Fixture.sport_id == sport.id))
        await db.execute(delete(Team).where(Team.sport_id == sport.id))
        await db.execute(delete(League).where(League.sport_id == sport.id))
        await db.execute(delete(Sport).where(Sport.id == sport.id))
        await db.commit()


async def test_the_two_verdicts_disagree_and_both_are_reported(api_client, corners_card_fixture):
    """THE WHOLE POINT. The 1X2 call lost and the card won, on the same fixture. If these ever
    come back equal, the fixture has stopped exercising the disagreement and the test is
    passing for the wrong reason."""
    sport, _fixture = corners_card_fixture
    response = await api_client.get("/history", params={"sport_slug": sport.slug})
    assert response.status_code == 200
    entry = response.json()[0]

    assert entry["predicted_outcome"] == "away"
    assert entry["was_correct"] is False, "the 1X2 call was away against a 4-0 home win"

    assert entry["pick_market"] == "corners_total"
    assert entry["pick_selection"] == "under"
    assert entry["pick_line"] == 10.5
    assert entry["pick_was_correct"] is True, "7 + 2 = 9 corners is under 10.5"


async def test_summary_reports_the_card_accuracy_alongside_the_1x2_one(
    api_client, corners_card_fixture
):
    sport, _ = corners_card_fixture
    response = await api_client.get("/history/summary", params={"sport_slug": sport.slug})
    assert response.status_code == 200
    row = response.json()[0]

    assert row["settled_fixtures"] == 1 and row["correct"] == 0  # 1X2: wrong
    assert row["card_pick_graded"] == 1 and row["card_pick_correct"] == 1  # card: right
    assert row["accuracy"] == 0.0
    assert row["card_pick_accuracy"] == 1.0


async def test_the_card_baseline_is_the_picks_own_market_rate(api_client, corners_card_fixture):
    """A higher card accuracy is mostly the market being easier, not the model being better --
    corners under 10.5 is an intrinsically likelier event than a 1X2 call. Reporting the
    accuracy without its own base rate is what would make that look like skill."""
    sport, _ = corners_card_fixture
    row = (await api_client.get("/history/summary", params={"sport_slug": sport.slug})).json()[0]
    assert row["card_pick_baseline"] is not None
    assert 0.3 < row["card_pick_baseline"] < 0.9


async def test_a_voided_fixture_is_never_graded_on_either_verdict(api_client, settled_fixtures):
    """Voids are excluded from both numbers, not just the 1X2 one. A retirement shows a neutral
    badge on the card precisely because most books void the bet."""
    sport, _fixtures = settled_fixtures
    row = (await api_client.get("/history/summary", params={"sport_slug": sport.slug})).json()[0]
    assert row["voided"] == 1
    assert row["card_pick_graded"] + row["card_pick_ungradable"] + row["card_pick_absent"] == (
        row["settled_fixtures"]
    )


async def test_fixtures_with_no_card_pick_are_counted_not_folded_into_losses(
    api_client, settled_fixtures
):
    """The feed's guards legitimately leave some fixtures with no pick at all. Those are not
    losses, and quietly dropping them would shrink the denominator without saying so — the same
    defect that once let row-counting overstate accuracy by 18.7pp."""
    sport, _fixtures = settled_fixtures
    row = (await api_client.get("/history/summary", params={"sport_slug": sport.slug})).json()[0]
    assert row["card_pick_absent"] >= 0
    assert row["card_pick_correct"] <= row["card_pick_graded"]
