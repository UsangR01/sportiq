from fastapi import APIRouter, Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.database import get_db
from app.fixtures.models import Fixture, FixtureLiveState, Team
from app.history.models import MatchResult, Outcome
from app.history.schemas import HistoryEntry, HistorySummary, ModelStats
from app.predictions.models import ModelRegistry, Prediction
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
    db: AsyncSession = Depends(get_db),
):
    """Real, live model accuracy per sport, measured on settled fixtures.

    Deliberately distinct from GET /stats/model, which reports what a model scored on its own
    held-out TEST SET at training time. This is how the served predictions have actually fared
    since. Divergence between the two is the whole point — it's the gap between a lab number
    and a live one, and it's the number worth trusting.

    Aggregated in Python rather than SQL: "was the pick correct" means comparing the argmax of
    three probability columns against an enum, which is expressible in SQL but markedly less
    readable, and the settled-fixture volume here is thousands, not millions."""
    rows = (await db.execute(_history_query(sport_slug, league_slug))).all()

    totals: dict[str, dict[str, int]] = {}
    for outcome, prediction, _fixture_id, sport, _league, _home, _away, result_type in rows:
        bucket = totals.setdefault(sport, {"settled": 0, "correct": 0, "voided": 0})
        if result_type is not None:
            bucket["voided"] += 1
            continue
        bucket["settled"] += 1
        if _RESULT_BY_OUTCOME[_predicted_outcome(prediction)] == outcome.result:
            bucket["correct"] += 1

    return [
        HistorySummary(
            sport_slug=sport,
            settled_fixtures=counts["settled"],
            correct=counts["correct"],
            # Guard the divide: a sport whose every settled fixture was voided has a real zero
            # denominator, which must not be turned into a fabricated accuracy.
            accuracy=(counts["correct"] / counts["settled"]) if counts["settled"] else 0.0,
            voided=counts["voided"],
        )
        for sport, counts in sorted(totals.items())
    ]


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
