import math

from fastapi import APIRouter, Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.database import get_db
from app.fixtures.models import Fixture, FixtureLiveState, Team
from app.history.models import MatchResult, Outcome
from app.history.schemas import HistoryEntry, HistorySummary, ModelStats
from app.predictions.models import ModelRegistry, Prediction, PredictionKind
from app.sports.models import League, Sport

router = APIRouter(tags=["history"])

_optional_bearer = HTTPBearer(auto_error=False)

_RESULT_BY_OUTCOME = {
    "home": MatchResult.HOME_WIN,
    "draw": MatchResult.DRAW,
    "away": MatchResult.AWAY_WIN,
}


def _predicted_outcome(prediction: Prediction) -> str:
    """The model's 1X2 call — the argmax of home/draw/away.

    draw_prob is None for two-outcome sports (tennis, NBA), where a draw isn't a possible
    result at all, so it's excluded rather than treated as a zero-probability option."""
    candidates = [("home", prediction.home_prob), ("away", prediction.away_prob)]
    if prediction.draw_prob is not None:
        candidates.append(("draw", prediction.draw_prob))
    return max(candidates, key=lambda pair: pair[1])[0]


# Below this, an accuracy is noise dressed as a result and is reported as "not enough data".
MIN_REPORTABLE_N = 30
# Standard normal quantiles for a one-sided alpha=0.05 test at 80% power, used for the
# minimum detectable effect. Hard-coded rather than pulled from scipy, which is not a
# dependency of the serving path.
_Z_ALPHA, _Z_BETA = 1.645, 0.842


def _wilson_interval(correct: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion.

    Wilson rather than the normal approximation because it stays inside [0, 1] and keeps
    sensible coverage at small n — which is exactly where this endpoint operates today
    (n=139 football, n=77 tennis). The normal approximation produces intervals that can run
    past 1.0 at these sizes, which would be visibly wrong in an API response."""
    if n == 0:
        return 0.0, 0.0
    p = correct / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def _detectable_effect(p: float, n: int) -> float:
    """Smallest true effect this sample could detect at 80% power.

    Reported alongside every accuracy because the two together are the honest statement. On
    2026-08-10 football could detect 10.5pp against a believed edge of 4.1pp, so the accuracy
    figure could not settle the question in either direction — and without this field beside
    it, it reads as though it had."""
    if n == 0:
        return 1.0
    return (_Z_ALPHA + _Z_BETA) * math.sqrt(max(p * (1 - p), 1e-9) / n)


def _history_query(sport_slug: str | None, league_slug: str | None):
    """Settled fixtures the model actually made a call on, newest first.

    Both joins are inner by design: a fixture we never predicted has no place in a record of
    how the model performed, and only genuinely settled Outcome rows count as results."""
    home_team, away_team = aliased(Team), aliased(Team)
    stmt = (
        select(
            Outcome,
            Prediction,
            Fixture.id,
            Sport.slug,
            League.slug,
            home_team.name,
            away_team.name,
            FixtureLiveState.result_type,
        )
        .join(Fixture, Fixture.id == Outcome.fixture_id)
        .join(Prediction, Prediction.fixture_id == Fixture.id)
        .join(Sport, Sport.id == Fixture.sport_id)
        .join(League, League.id == Fixture.league_id)
        .join(home_team, home_team.id == Fixture.home_team_id)
        .join(away_team, away_team.id == Fixture.away_team_id)
        # Outer: most fixtures have no live-state row carrying a result_type at all.
        .outerjoin(FixtureLiveState, FixtureLiveState.fixture_id == Fixture.id)
        .order_by(Outcome.settled_at.desc())
    )
    if sport_slug:
        stmt = stmt.where(Sport.slug == sport_slug)
    if league_slug:
        stmt = stmt.where(League.slug == league_slug)
    return stmt


@router.get("/history", response_model=list[HistoryEntry])
async def get_history(
    sport_slug: str | None = None,
    league_slug: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
    db: AsyncSession = Depends(get_db),
):
    """Real settled prediction history — every completed fixture the model called, and whether
    it was right (TDD §4.1 HIST-01, guest-accessible).

    This was a 501 for a long time, and legitimately so: it needs settled outcomes, and nothing
    wrote to the outcomes table until _maybe_settle_outcome existed. Those rows are real now,
    so this reports actual results instead of a placeholder.

    Voided fixtures — a tennis retirement or walkover, where bookmakers generally void bets —
    are omitted here and counted separately in /history/summary, matching the mobile feed's
    existing refusal to score them as either a win or a loss.

    The authenticated variant (the caller's own saved picks) still needs a watchlist table,
    which doesn't exist yet — see CLAUDE.md. Auth is accepted and ignored rather than rejected,
    so the guest-accessible contract holds either way."""
    # Voided fixtures are excluded in SQL, BEFORE the limit — filtering them in Python
    # afterwards applies `limit` to the unfiltered set, so a page whose most recent rows happen
    # to be retirements comes back short or entirely empty. Caught live: `?limit=4` for tennis
    # returned zero entries because the four newest settled fixtures were all retirements.
    # NULL covers both "no live-state row at all" (most sports) and "live state with no
    # result_type" (a normally-played-out match).
    stmt = (
        _history_query(sport_slug, league_slug)
        .where(FixtureLiveState.result_type.is_(None))
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()

    entries: list[HistoryEntry] = []
    for outcome, prediction, fixture_id, sport, league, home, away, _result_type in rows:
        predicted = _predicted_outcome(prediction)
        probability = {
            "home": prediction.home_prob,
            "draw": prediction.draw_prob or 0.0,
            "away": prediction.away_prob,
        }[predicted]
        entries.append(
            HistoryEntry(
                fixture_id=fixture_id,
                sport_slug=sport,
                league_slug=league,
                home_team=home,
                away_team=away,
                model_version=prediction.model_version,
                predicted_probability=probability,
                confidence_tier=prediction.confidence_tier.value,
                predicted_outcome=predicted,
                result=outcome.result.value,
                was_correct=_RESULT_BY_OUTCOME[predicted] == outcome.result,
                settled_at=outcome.settled_at,
            )
        )
    return entries


@router.get("/history/summary", response_model=list[HistorySummary])
async def get_history_summary(
    sport_slug: str | None = None,
    league_slug: str | None = None,
    kind: str = Query(
        PredictionKind.PRE_MATCH.value,
        description=(
            "Which predictions to score. Defaults to pre_match — the only kind that evidences "
            "forecasting skill. 'retrodiction' and 'unknown' are available explicitly but must "
            "never be presented as a track record."
        ),
    ),
    db: AsyncSession = Depends(get_db),
):
    """Real, live model accuracy per sport, measured on settled fixtures.

    Deliberately distinct from GET /stats/model, which reports what a model scored on its own
    held-out TEST SET at training time. This is how the served predictions have actually fared
    since. Divergence between the two is the whole point — it's the gap between a lab number
    and a live one, and it's the number worth trusting.

    SCOPED TO pre_match BY DEFAULT. Both prediction paths write to the same table, and until
    predictions.kind existed this endpoint averaged them together and reported the result as
    one accuracy. Measured on 2026-08-10, that mixing moved football from 56.1% to 53.3% and
    tennis from 63.6% to 61.7% — DOWNWARD, which is the opposite of the expected direction and
    worth stating: retrodictions score worse, not better, because assemble_from_game_log has a
    leakage guard and thinner features. The problem was never hindsight flattering the model;
    it was averaging two populations with different feature quality and reporting neither.

    Every accuracy carries its sample size, a Wilson interval and its minimum detectable effect
    (docs/history-metrics-spec.md §6). Without them a percentage reads as a verdict: football's
    139 settled pre-match predictions can only detect a 10.5pp effect, against a believed edge
    of 4.1pp, so the figure cannot currently settle the question either way.

    Aggregated in Python rather than SQL: "was the pick correct" means comparing the argmax of
    three probability columns against an enum, which is expressible in SQL but markedly less
    readable, and the settled-fixture volume here is thousands, not millions."""
    rows = (await db.execute(_history_query(sport_slug, league_slug))).all()

    totals: dict[str, dict[str, int]] = {}
    for outcome, prediction, _fixture_id, sport, _league, _home, _away, result_type in rows:
        bucket = totals.setdefault(
            sport, {"settled": 0, "correct": 0, "voided": 0, "excluded_kind": 0}
        )
        if result_type is not None:
            bucket["voided"] += 1
            continue
        if prediction.kind.value != kind:
            # Counted, not silently dropped: a denominator that quietly shrinks is how a
            # partial population starts looking like a complete one.
            bucket["excluded_kind"] += 1
            continue
        bucket["settled"] += 1
        if _RESULT_BY_OUTCOME[_predicted_outcome(prediction)] == outcome.result:
            bucket["correct"] += 1

    summaries = []
    for sport, counts in sorted(totals.items()):
        n, correct = counts["settled"], counts["correct"]
        # Guard the divide: a sport whose every settled fixture was voided or excluded has a
        # real zero denominator, which must not be turned into a fabricated accuracy.
        accuracy = (correct / n) if n else 0.0
        ci_low, ci_high = _wilson_interval(correct, n)
        summaries.append(
            HistorySummary(
                sport_slug=sport,
                settled_fixtures=n,
                correct=correct,
                accuracy=accuracy,
                voided=counts["voided"],
                kind=kind,
                accuracy_ci_low=ci_low,
                accuracy_ci_high=ci_high,
                detectable_effect=_detectable_effect(accuracy, n),
                sufficient_sample=n >= MIN_REPORTABLE_N,
                excluded_unknown_provenance=counts["excluded_kind"],
            )
        )
    return summaries


@router.get("/stats/model", response_model=list[ModelStats])
async def get_model_stats(sport_slug: str | None = None, db: AsyncSession = Depends(get_db)):
    """Model performance summary per sport (TDD §4.1) — the currently *active* model per
    sport, since that's what's actually serving predictions right now. Model promotion
    (models_registry.is_active flip) is a DB update per TDD §3.1, so this always reflects
    whichever version is live without a code change.

    Reports TRAINING-time metrics. For how those predictions have actually performed on real
    settled fixtures since, see /history/summary."""
    stmt = (
        select(ModelRegistry, Sport.slug)
        .join(Sport, Sport.id == ModelRegistry.sport_id)
        .where(ModelRegistry.is_active.is_(True))
    )
    if sport_slug:
        stmt = stmt.where(Sport.slug == sport_slug)

    rows = (await db.execute(stmt)).all()
    return [
        ModelStats(
            sport_slug=slug,
            model_version=model.version,
            accuracy=model.accuracy,
            rps_score=model.rps_score,
            roi_simulation=model.roi_simulation,
            trained_at=model.trained_at,
        )
        for model, slug in rows
    ]
