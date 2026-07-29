import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.database import get_db
from app.core.redis import get_redis
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.models_ml.markets import CORNERS_LINES, GOALS_LINES, double_chance_probs, over_under_probs
from app.odds.models import Odds, OddsMarket
from app.picks.schemas import PickResponse
from app.picks.service import (
    OutcomeCandidate,
    best_available_odds,
    best_outcome,
    best_outcome_from_candidates,
    best_totals_odds,
    compute_expected_value,
)
from app.predictions.models import Prediction
from app.sports.models import Sport

router = APIRouter(tags=["picks"])

PICKS_CACHE_TTL_SECONDS = 180
PICKS_LOOKAHEAD_DAYS = 7

# API market name -> Odds.market DB value. "goals_total"/"corners_total" are the client-facing
# names (matching this feature's own naming, see CLAUDE.md); "total" is the DB enum's existing
# value (originally meant generically, in practice always goals — see app/odds/models.py).
_DB_MARKET_BY_API_MARKET = {
    "h2h": OddsMarket.H2H,
    "double_chance": OddsMarket.DOUBLE_CHANCE,
    "goals_total": OddsMarket.TOTAL,
    "corners_total": OddsMarket.CORNERS_TOTAL,
}
_VALID_LINES_BY_API_MARKET = {"goals_total": GOALS_LINES, "corners_total": CORNERS_LINES}


def _build_outcome(
    market: str, line: float | None, prediction: Prediction, odds_rows: list[dict]
) -> OutcomeCandidate | None:
    """Dispatches to the right candidate-building logic per market — all four markets reduce
    to the same "pick the model's favourite among whichever outcomes have real odds" shape
    (best_outcome_from_candidates), they just differ in where the probability and odds come
    from."""
    if market == "h2h":
        best_odds = best_available_odds(odds_rows)
        return best_outcome(
            prediction.home_prob,
            prediction.draw_prob,
            prediction.away_prob,
            best_odds["home"],
            best_odds["draw"],
            best_odds["away"],
        )

    if market == "double_chance":
        best_odds = best_available_odds(odds_rows)  # reuses home/away columns, see service.py
        home_or_draw_prob, away_or_draw_prob = double_chance_probs(
            prediction.home_prob, prediction.draw_prob, prediction.away_prob
        )
        return best_outcome_from_candidates(
            [
                OutcomeCandidate("1X", home_or_draw_prob, best_odds["home"]),
                OutcomeCandidate("X2", away_or_draw_prob, best_odds["away"]),
            ]
        )

    # goals_total / corners_total
    if market == "goals_total":
        expected_total = (
            prediction.xg_home + prediction.xg_away
            if prediction.xg_home is not None and prediction.xg_away is not None
            else None
        )
    else:
        expected_total = (
            prediction.corners_xg_home + prediction.corners_xg_away
            if prediction.corners_xg_home is not None and prediction.corners_xg_away is not None
            else None
        )
    under_prob, over_prob = over_under_probs(expected_total, (line,))[line]
    over_odds, under_odds = best_totals_odds(odds_rows, line)
    return best_outcome_from_candidates(
        [
            OutcomeCandidate("over", over_prob, over_odds),
            OutcomeCandidate("under", under_prob, under_odds),
        ]
    )


@router.get("/picks", response_model=list[PickResponse])
async def get_picks(
    min_odds: float = Query(..., ge=1.01, le=20.0),
    sport_slug: str | None = None,
    market: str = Query("h2h", pattern="^(h2h|double_chance|goals_total|corners_total)$"),
    line: float | None = Query(None),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    if market in _VALID_LINES_BY_API_MARKET:
        valid_lines = _VALID_LINES_BY_API_MARKET[market]
        if line is None:
            raise HTTPException(422, detail=f"market={market!r} requires a `line` query param")
        if line not in valid_lines:
            raise HTTPException(
                422, detail=f"market={market!r} only supports line in {valid_lines}"
            )
    elif line is not None:
        raise HTTPException(422, detail=f"`line` is not applicable to market={market!r}")

    db_market = _DB_MARKET_BY_API_MARKET[market]

    redis = get_redis()
    cache_key = f"picks:{sport_slug or 'all'}:{market}:{line}:{min_odds}:{limit}"

    cached = await redis.get(cache_key)
    if cached is not None:
        return [PickResponse.model_validate(item) for item in json.loads(cached)]

    now = datetime.now(UTC)
    horizon = now + timedelta(days=PICKS_LOOKAHEAD_DAYS)

    home_team = aliased(Team)
    away_team = aliased(Team)

    stmt = (
        select(Fixture, Sport.slug, home_team.name, away_team.name)
        .join(Sport, Sport.id == Fixture.sport_id)
        .join(home_team, home_team.id == Fixture.home_team_id)
        .join(away_team, away_team.id == Fixture.away_team_id)
        .where(
            Fixture.status == FixtureStatus.SCHEDULED,
            Fixture.kickoff_utc >= now,
            Fixture.kickoff_utc <= horizon,
        )
    )
    if sport_slug:
        stmt = stmt.where(Sport.slug == sport_slug)

    fixture_rows = (await db.execute(stmt)).all()
    if not fixture_rows:
        await redis.set(cache_key, json.dumps([]), ex=PICKS_CACHE_TTL_SECONDS)
        return []

    fixture_ids = [row[0].id for row in fixture_rows]

    odds_rows = (
        (
            await db.execute(
                select(Odds).where(Odds.fixture_id.in_(fixture_ids), Odds.market == db_market)
            )
        )
        .scalars()
        .all()
    )
    odds_by_fixture: dict = {}
    for o in odds_rows:
        odds_by_fixture.setdefault(o.fixture_id, []).append(
            {
                "home_odds": o.home_odds,
                "draw_odds": o.draw_odds,
                "away_odds": o.away_odds,
                "line": o.line,
                "over_odds": o.over_odds,
                "under_odds": o.under_odds,
            }
        )

    prediction_rows = (
        (await db.execute(select(Prediction).where(Prediction.fixture_id.in_(fixture_ids))))
        .scalars()
        .all()
    )
    latest_prediction_by_fixture: dict = {}
    for p in prediction_rows:
        existing = latest_prediction_by_fixture.get(p.fixture_id)
        if existing is None or p.created_at > existing.created_at:
            latest_prediction_by_fixture[p.fixture_id] = p

    picks: list[PickResponse] = []
    for fixture, sport_slug_val, home_name, away_name in fixture_rows:
        prediction = latest_prediction_by_fixture.get(fixture.id)
        if prediction is None:
            continue

        outcome = _build_outcome(market, line, prediction, odds_by_fixture.get(fixture.id, []))
        if outcome is None:
            continue
        # The threshold is a promise about the pick actually shown, not "some market on this
        # fixture happens to clear it" — a fixture whose favoured selection is home @ 1.30 must
        # not appear just because its away odds are 6.00 (the bug the user reported: sub-
        # threshold picks were visible because this used to check the max odds across all three
        # markets instead of the recommended outcome's own odds).
        if outcome.odds < min_odds:
            continue

        picks.append(
            PickResponse(
                fixture_id=fixture.id,
                sport_slug=sport_slug_val,
                home_team=home_name,
                away_team=away_name,
                kickoff_utc=fixture.kickoff_utc,
                market=market,
                selection=outcome.selection,
                line=line,
                odds=outcome.odds,
                model_probability=outcome.probability,
                expected_value=compute_expected_value(outcome.probability, outcome.odds),
                confidence_tier=prediction.confidence_tier.value,
            )
        )

    picks.sort(key=lambda p: p.model_probability, reverse=True)
    picks = picks[:limit]

    await redis.set(
        cache_key,
        json.dumps([p.model_dump(mode="json") for p in picks]),
        ex=PICKS_CACHE_TTL_SECONDS,
    )
    return picks
