"""One fixture must not show two different picks on two screens.

Reported as "the live page read the same as the saved page - 1X @ 1.17", where the card for the
same match read HOME @ 1.55. best_pick is recomputed on every request from the candidates that
clear the CALLER's floors, and the Live tab passed none -- so a 1.17 price it ranked first was
one the Picks feed had already excluded at the 1.20 default. Identical root cause to the
watchlist receipt bug: one fixture, two callers, two answers.

The Live tab now passes the user's floors. This file pins the backend half that makes that safe:
a live match is never dropped for failing them, because a live-scores screen losing the match
being played would be a worse bug than the one being fixed.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.main import create_app
from app.odds.models import Odds, OddsMarket
from app.predictions.models import ConfidenceTier, Prediction, PredictionKind
from app.sports.models import League, Sport


@pytest.fixture
async def api_client():
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
async def live_fixture_priced_below_the_floor():
    """A LIVE match whose only real candidate is priced at 1.17 -- under the 1.20 default.

    Modelled on the reported fixture: the double chance is both the most likely outcome and too
    short to clear the odds slider.
    """
    async with async_session_factory() as db:
        slug = f"test-live-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Test Live", model_type="test", active=True)
        db.add(sport)
        await db.flush()
        league = League(
            sport_id=sport.id, slug=f"l-{slug}", name="L", country=None, tier=1, active=True
        )
        db.add(league)
        await db.flush()
        home = Team(sport_id=sport.id, league_id=league.id, name="Home", external_id=f"h{slug}")
        away = Team(sport_id=sport.id, league_id=league.id, name="Away", external_id=f"a{slug}")
        db.add_all([home, away])
        await db.flush()
        now = datetime.now(UTC)
        fixture = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            external_id=f"fx-{slug}",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=now - timedelta(minutes=30),
            status=FixtureStatus.LIVE,
            season="2026",
        )
        db.add(fixture)
        await db.flush()
        db.add(
            Prediction(
                fixture_id=fixture.id,
                model_version="test_v1",
                home_prob=0.655,
                draw_prob=0.161,
                away_prob=0.184,
                confidence_tier=ConfidenceTier.MEDIUM,
                created_at=now - timedelta(hours=3),
                kind=PredictionKind.PRE_MATCH,
            )
        )
        # 1X at 1.17 -- the short price the card excludes.
        db.add(
            Odds(
                fixture_id=fixture.id,
                bookmaker="test-book",
                market=OddsMarket.DOUBLE_CHANCE,
                home_odds=1.17,
                away_odds=4.50,
                updated_at=now,
            )
        )
        # EVERY candidate has to be priced, and priced short, or this fixture cannot exercise
        # the drop at all: min_odds deliberately does NOT reject a pick with no odds (the
        # tennis exemption), so an unpriced h2h/home would sail through the floor and the
        # fixture would survive whether or not the live exemption existed. The first version of
        # this test made exactly that mistake and passed against unfixed source.
        db.add(
            Odds(
                fixture_id=fixture.id,
                bookmaker="test-book",
                market=OddsMarket.H2H,
                home_odds=1.15,
                draw_odds=1.10,
                away_odds=1.12,
                updated_at=now,
            )
        )
        db.add(
            FixtureLiveState(
                fixture_id=fixture.id,
                home_score=1,
                away_score=0,
                match_minute=30,
                status="live",
                last_updated_utc=now,
            )
        )
        await db.commit()
        await db.refresh(sport)
        await db.refresh(fixture)

    yield sport, fixture

    async with async_session_factory() as db:
        await db.execute(delete(Odds).where(Odds.fixture_id == fixture.id))
        await db.execute(delete(Prediction).where(Prediction.fixture_id == fixture.id))
        await db.execute(delete(FixtureLiveState).where(FixtureLiveState.fixture_id == fixture.id))
        await db.execute(delete(Fixture).where(Fixture.sport_id == sport.id))
        await db.execute(delete(Team).where(Team.sport_id == sport.id))
        await db.execute(delete(League).where(League.sport_id == sport.id))
        await db.execute(delete(Sport).where(Sport.id == sport.id))
        await db.commit()


async def test_a_live_match_survives_a_floor_its_pick_cannot_clear(
    api_client, live_fixture_priced_below_the_floor
):
    """THE REGRESSION. Without the exemption the Live tab would empty itself the moment it
    started passing the user's own sliders."""
    sport, fixture = live_fixture_priced_below_the_floor
    response = await api_client.get(
        "/fixtures",
        params={"sport_slug": sport.slug, "min_probability": 0.6, "min_odds": 1.2},
    )
    assert response.status_code == 200
    row = next((r for r in response.json() if r["id"] == str(fixture.id)), None)
    assert row is not None, "a live match must never be dropped for failing a betting floor"
    assert row["status"] == "live"
    assert row["live_state"]["home_score"] == 1, "the score is the point of the screen"


async def test_the_short_priced_pick_is_withheld_rather_than_shown(
    api_client, live_fixture_priced_below_the_floor
):
    """The other half. Surviving the filter must not mean the excluded pick comes back -- that
    would restore the very disagreement being fixed, just from the other direction."""
    sport, fixture = live_fixture_priced_below_the_floor
    response = await api_client.get(
        "/fixtures",
        params={"sport_slug": sport.slug, "min_probability": 0.6, "min_odds": 1.2},
    )
    row = next(r for r in response.json() if r["id"] == str(fixture.id))
    pick = row["best_pick"]
    assert pick is None or pick["odds"] is None or pick["odds"] >= 1.2


async def test_both_screens_agree_once_they_pass_the_same_floors(
    api_client, live_fixture_priced_below_the_floor
):
    """What the user actually observed, asserted directly: the Live tab's request and the Picks
    feed's request must resolve to the SAME pick for the same fixture."""
    sport, fixture = live_fixture_priced_below_the_floor
    floors = {"min_probability": 0.6, "min_odds": 1.2}

    live = await api_client.get(
        "/fixtures", params={"status": "live", "sport_slug": sport.slug, **floors}
    )
    picks = await api_client.get("/fixtures", params={"sport_slug": sport.slug, **floors})

    live_row = next(r for r in live.json() if r["id"] == str(fixture.id))
    picks_row = next((r for r in picks.json() if r["id"] == str(fixture.id)), None)
    assert picks_row is not None
    assert live_row["best_pick"] == picks_row["best_pick"]


async def test_the_unfloored_call_is_what_used_to_disagree(
    api_client, live_fixture_priced_below_the_floor
):
    """Proves the fixture genuinely exercises the bug rather than passing vacuously: asked with
    NO floors -- the Live tab's old shape -- the 1.17 double chance is exactly what surfaces."""
    sport, fixture = live_fixture_priced_below_the_floor
    response = await api_client.get("/fixtures", params={"sport_slug": sport.slug})
    row = next(r for r in response.json() if r["id"] == str(fixture.id))
    assert row["best_pick"]["market"] == "double_chance"
    assert row["best_pick"]["odds"] == pytest.approx(1.17)
