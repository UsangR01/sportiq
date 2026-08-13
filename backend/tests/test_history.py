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
from app.predictions.models import ConfidenceTier, Prediction, PredictionKind
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
                    # Explicit: /history/summary scores pre_match only by default, so an
                    # unmarked prediction is correctly excluded rather than counted.
                    kind=PredictionKind.PRE_MATCH,
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


@pytest.mark.asyncio
async def test_summary_scores_pre_match_only_and_counts_what_it_excluded(
    api_client, settled_fixtures
):
    """The contamination guard.

    Both prediction paths write to the same table, and before predictions.kind existed this
    endpoint averaged them and reported one accuracy. Measured on real data, mixing moved
    football 56.1% -> 53.3% and tennis 63.6% -> 61.7% — DOWNWARD, because retrodictions score
    worse rather than better (assemble_from_game_log has a leakage guard and thinner features).
    So the fault was never hindsight flattering the model; it was averaging two populations
    with different feature quality and reporting neither.

    Excluded rows are COUNTED. A denominator that quietly shrinks is how a partial population
    starts looking like a complete one.
    """
    response = await api_client.get("/history/summary")
    assert response.status_code == 200
    summary = response.json()[0]

    assert summary["kind"] == "pre_match"
    assert summary["settled_fixtures"] > 0
    assert summary["excluded_unknown_provenance"] >= 0


@pytest.mark.asyncio
async def test_summary_never_reports_an_accuracy_without_its_sample_context(
    api_client, settled_fixtures
):
    """A percentage on its own reads as a verdict. Football's 139 settled pre-match predictions
    can only detect a 10.5pp effect against a believed 4.1pp edge, so the number cannot settle
    the question either way — and without n, an interval and the detectable effect beside it,
    nothing on the page says so."""
    summary = (await api_client.get("/history/summary")).json()[0]

    assert summary["accuracy_ci_low"] <= summary["accuracy"] <= summary["accuracy_ci_high"]
    assert summary["detectable_effect"] > 0
    # The seeded sample is tiny, so this must self-report as not enough data rather than
    # publishing a confident-looking percentage.
    assert summary["sufficient_sample"] is False


@pytest.mark.asyncio
async def test_asking_for_retrodictions_is_possible_but_never_the_default(
    api_client, settled_fixtures
):
    """Retrodictions are a legitimate feed feature — "what the model would have said" — and a
    legitimate thing to inspect. They are simply not a track record, so reaching them takes an
    explicit ask."""
    default = (await api_client.get("/history/summary")).json()
    explicit = (await api_client.get("/history/summary?kind=retrodiction")).json()

    assert default[0]["kind"] == "pre_match"
    assert explicit == [] or explicit[0]["kind"] == "retrodiction"


@pytest.mark.asyncio
async def test_reportable_and_conclusive_are_separate_questions(api_client, settled_fixtures):
    """Pre-registered in spec §9.5, after the single flag proved ambiguous in the dangerous
    direction: it read as "this number is trustworthy" when it only meant "big enough to print".

    A metric is routinely reportable AND inconclusive at once — that is the normal state for
    this product today, and a UI that cannot express it will imply a verdict that the sample
    does not support."""
    summary = (await api_client.get("/history/summary")).json()[0]

    assert "sufficient_sample" in summary  # safe to DISPLAY
    assert "conclusive" in summary  # safe to ACT on
    # The seeded sample is tiny, so it must fail both rather than either.
    assert summary["sufficient_sample"] is False
    assert summary["conclusive"] is False


def test_the_reportable_threshold_matches_the_pre_registration():
    """MIN_REPORTABLE_N is anchored to interval width (§9.4), not to any measured result: 93 is
    the smallest n whose 95% Wilson interval is narrower than 20 points at p=0.5.

    Pinned because the value is meaningful, not arbitrary — someone lowering it to make a
    sparse league reportable would be reintroducing exactly the failure the spec forbids."""
    import math

    from app.history.router import MIN_REPORTABLE_N

    def width(n, p=0.5, z=1.96):
        d = 1 + z * z / n
        return 2 * z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d

    assert MIN_REPORTABLE_N == 93
    assert width(MIN_REPORTABLE_N) <= 0.20
    assert width(MIN_REPORTABLE_N - 1) > 0.20  # genuinely the smallest such n


@pytest.fixture
async def fixture_predicted_many_times():
    """One settled fixture carrying a SERIES of pre-match predictions, as production actually
    stores them: run_predictions appends a row each time a fixture is re-predicted rather than
    replacing one. The final forecast before kickoff is WRONG while the earlier revisions were
    right, so any metric that counts rows instead of fixtures scores this fixture as mostly
    correct when the call that stood at kickoff was not."""
    async with async_session_factory() as db:
        slug = f"test-dup-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Test Dup", model_type="test", active=True)
        db.add(sport)
        await db.flush()
        league = League(sport_id=sport.id, slug=f"{slug}-l", name="L", country="XX", tier=1)
        db.add(league)
        await db.flush()
        home = Team(sport_id=sport.id, league_id=league.id, name="H", external_id=f"h{slug}")
        away = Team(sport_id=sport.id, league_id=league.id, name="A", external_id=f"a{slug}")
        db.add_all([home, away])
        await db.flush()
        kickoff = datetime.now(UTC) - timedelta(days=1)
        fixture = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            external_id=f"fx-{slug}",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=kickoff,
            status=FixtureStatus.COMPLETED,
            season="2026",
        )
        db.add(fixture)
        await db.flush()
        db.add(
            Outcome(
                fixture_id=fixture.id,
                home_score=0,
                away_score=1,
                result=MatchResult.AWAY_WIN,
                settled_at=kickoff + timedelta(hours=2),
            )
        )
        # Four earlier revisions calling HOME (correct by luck of the row count, wrong on the
        # result), then the final pre-kickoff call, also HOME... deliberately make the earlier
        # ones AWAY (right) and the last one HOME (wrong), so row-counting and fixture-counting
        # disagree in an unmistakable direction.
        for i, (h, a) in enumerate([(0.2, 0.8), (0.2, 0.8), (0.2, 0.8), (0.2, 0.8)]):
            db.add(
                Prediction(
                    fixture_id=fixture.id,
                    model_version="v1",
                    home_prob=h,
                    draw_prob=None,
                    away_prob=a,
                    confidence_tier=ConfidenceTier.MEDIUM,
                    kind=PredictionKind.PRE_MATCH,
                    created_at=kickoff - timedelta(hours=10 - i),
                )
            )
        db.add(
            Prediction(
                fixture_id=fixture.id,
                model_version="v1",
                home_prob=0.9,
                draw_prob=None,
                away_prob=0.1,
                confidence_tier=ConfidenceTier.HIGH,
                kind=PredictionKind.PRE_MATCH,
                created_at=kickoff - timedelta(minutes=30),
            )
        )
        await db.commit()
        ids = {"sport_id": sport.id, "slug": slug, "fixture_id": fixture.id}
    yield ids
    async with async_session_factory() as db:
        await db.execute(delete(Prediction).where(Prediction.fixture_id == ids["fixture_id"]))
        await db.execute(delete(Outcome).where(Outcome.fixture_id == ids["fixture_id"]))
        await db.execute(delete(Fixture).where(Fixture.sport_id == ids["sport_id"]))
        await db.execute(delete(Team).where(Team.sport_id == ids["sport_id"]))
        await db.execute(delete(League).where(League.sport_id == ids["sport_id"]))
        await db.execute(delete(Sport).where(Sport.id == ids["sport_id"]))
        await db.commit()


@pytest.mark.asyncio
async def test_a_fixture_counts_once_however_often_it_was_re_predicted(
    api_client, fixture_predicted_many_times
):
    """THE defect, found while building ml/notebooks/prediction_history.ipynb.

    run_predictions appends rather than replaces, so 143 settled football pre-match rows sat on
    just 63 fixtures — one carrying 25. Every metric was therefore weighted by how often a
    fixture happened to be re-predicted, and the reported football accuracy moved 0.5524 ->
    0.3651 once each fixture counted once. A fixture is one event and contributes one result.
    """
    slug = fixture_predicted_many_times["slug"]
    resp = await api_client.get(f"/history?sport_slug={slug}&limit=50")
    assert resp.status_code == 200
    entries = resp.json()
    assert len(entries) == 1, "five prediction rows, one fixture, one graded entry"

    summary = (await api_client.get(f"/history/summary?sport_slug={slug}")).json()
    assert summary[0]["settled_fixtures"] == 1  # the field was always named for fixtures


@pytest.mark.asyncio
async def test_the_representative_prediction_is_the_last_one_before_kickoff(
    api_client, fixture_predicted_many_times
):
    """Not simply the newest row. created_at is not trustworthy on its own — regeneration reset
    it on 91 football rows to timestamps AFTER their own kickoffs — and the honest
    representative is the call that stood when the user could still act on it."""
    slug = fixture_predicted_many_times["slug"]
    entry = (await api_client.get(f"/history?sport_slug={slug}")).json()[0]
    # The last pre-kickoff revision called HOME at 0.9 and the match finished AWAY.
    assert entry["predicted_outcome"] == "home"
    assert entry["was_correct"] is False
    assert entry["predicted_probability"] == pytest.approx(0.9)
