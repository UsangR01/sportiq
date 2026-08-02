"""GET /history and /history/summary — real settled prediction performance.

Both were a 501 until settled Outcome rows existed. They do now, so these cover the parts that
are easy to get subtly wrong: scoring the right market, excluding voided fixtures without
corrupting the denominator, and applying the row limit AFTER exclusion rather than before.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.history.models import MatchResult, Outcome
from app.main import app
from app.predictions.models import ConfidenceTier, Prediction
from app.sports.models import League, Sport


@pytest.fixture
async def api_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def settled_fixtures():
    """Four settled fixtures under an isolated test sport: two correct calls, one wrong, and
    one voided retirement. Deliberately ordered so the VOIDED one settles most recently — that
    ordering is what exposed the limit-before-exclusion bug against real data."""
    async with async_session_factory() as db:
        slug = f"test-hist-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Test Hist", model_type="test", active=True)
        db.add(sport)
        await db.flush()
        league = League(
            sport_id=sport.id, slug=f"l-{slug}", name="L", country=None, tier=1, active=True
        )
        db.add(league)
        await db.flush()
        home = Team(sport_id=sport.id, league_id=league.id, name="Home", external_id="h")
        away = Team(sport_id=sport.id, league_id=league.id, name="Away", external_id="a")
        db.add_all([home, away])
        await db.flush()

        now = datetime.now(UTC)
        # (home_prob, away_prob, real result, is_voided, settled offset in hours)
        spec = [
            (0.80, 0.20, MatchResult.HOME_WIN, False, 4),  # correct
            (0.30, 0.70, MatchResult.AWAY_WIN, False, 3),  # correct
            (0.75, 0.25, MatchResult.AWAY_WIN, False, 2),  # wrong
            (0.90, 0.10, MatchResult.HOME_WIN, True, 1),  # voided (most recent)
        ]
        fixtures = []
        for i, (hp, ap, result, voided, hours_ago) in enumerate(spec):
            fixture = Fixture(
                sport_id=sport.id,
                league_id=league.id,
                external_id=f"hist-{slug}-{i}",
                home_team_id=home.id,
                away_team_id=away.id,
                kickoff_utc=now - timedelta(days=i + 1),
                status=FixtureStatus.COMPLETED,
                season="2026",
            )
            db.add(fixture)
            await db.flush()
            db.add(
                Prediction(
                    fixture_id=fixture.id,
                    model_version="test_v1",
                    home_prob=hp,
                    draw_prob=None,  # two-outcome sport
                    away_prob=ap,
                    confidence_tier=ConfidenceTier.HIGH,
                    created_at=now,
                )
            )
            db.add(
                Outcome(
                    fixture_id=fixture.id,
                    home_score=2 if result is MatchResult.HOME_WIN else 0,
                    away_score=0 if result is MatchResult.HOME_WIN else 2,
                    result=result,
                    settled_at=now - timedelta(hours=hours_ago),
                )
            )
            db.add(
                FixtureLiveState(
                    fixture_id=fixture.id,
                    home_score=2 if result is MatchResult.HOME_WIN else 0,
                    away_score=0 if result is MatchResult.HOME_WIN else 2,
                    status="completed",
                    result_type="retired" if voided else None,
                    last_updated_utc=now,
                )
            )
            fixtures.append(fixture)
        await db.commit()
        await db.refresh(sport)

    yield sport, fixtures

    async with async_session_factory() as db:
        ids = [f.id for f in fixtures]
        await db.execute(delete(Outcome).where(Outcome.fixture_id.in_(ids)))
        await db.execute(delete(Prediction).where(Prediction.fixture_id.in_(ids)))
        await db.execute(delete(FixtureLiveState).where(FixtureLiveState.fixture_id.in_(ids)))
        await db.execute(delete(Fixture).where(Fixture.sport_id == sport.id))
        await db.execute(delete(Team).where(Team.sport_id == sport.id))
        await db.execute(delete(League).where(League.sport_id == sport.id))
        await db.execute(delete(Sport).where(Sport.id == sport.id))
        await db.commit()


async def test_history_is_no_longer_a_501(api_client, settled_fixtures):
    sport, _ = settled_fixtures
    response = await api_client.get("/history", params={"sport_slug": sport.slug})
    assert response.status_code == 200


async def test_history_scores_each_call_against_the_real_result(api_client, settled_fixtures):
    sport, _ = settled_fixtures
    body = (await api_client.get("/history", params={"sport_slug": sport.slug})).json()

    assert len(body) == 3, "the voided fixture must not appear"
    assert [e["was_correct"] for e in body] == [False, True, True], "newest settled first"
    wrong = body[0]
    assert wrong["predicted_outcome"] == "home"
    assert wrong["result"] == "away_win"
    # predicted_probability is the model's confidence in the outcome it PICKED, not in home.
    assert wrong["predicted_probability"] == pytest.approx(0.75)


async def test_history_limit_applies_after_voided_are_excluded(api_client, settled_fixtures):
    """The real bug this guards: excluding voided fixtures in Python AFTER the SQL limit meant
    `limit` counted rows that were then thrown away. Live, `?limit=4` for tennis returned zero
    entries because the four newest settled fixtures were all retirements. Here the single
    voided fixture is the most recently settled, so limit=1 must still yield one real entry."""
    sport, _ = settled_fixtures
    body = (await api_client.get("/history", params={"sport_slug": sport.slug, "limit": 1})).json()
    assert len(body) == 1
    assert body[0]["was_correct"] is False  # the newest NON-voided fixture


async def test_history_summary_reports_accuracy_and_counts_voids_separately(
    api_client, settled_fixtures
):
    """A voided fixture must not count as either a win or a loss, and must not silently vanish
    from the denominator - it's reported on its own so the numbers stay auditable."""
    sport, _ = settled_fixtures
    body = (await api_client.get("/history/summary", params={"sport_slug": sport.slug})).json()

    assert len(body) == 1
    summary = body[0]
    assert summary["settled_fixtures"] == 3
    assert summary["correct"] == 2
    assert summary["accuracy"] == pytest.approx(2 / 3)
    assert summary["voided"] == 1
