import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.database import get_db
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team, TeamFeatures
from app.fixtures.schemas import (
    BestPick,
    ExtraMarketsResponse,
    FixtureDetail,
    FixtureSummary,
    LiveStateResponse,
    OddsLineResponse,
    PredictionResponse,
    TeamFeaturesResponse,
    TotalsProbability,
)
from app.models_ml.markets import CORNERS_LINES, GOALS_LINES, double_chance_probs, over_under_probs
from app.odds.models import Odds
from app.picks.service import best_available_odds, best_totals_odds
from app.predictions.models import Prediction
from app.sports.models import League, Sport

router = APIRouter(tags=["fixtures"])

_VALID_LINES_BY_MARKET = {"goals_total": GOALS_LINES, "corners_total": CORNERS_LINES}


@dataclass(frozen=True)
class _MarketCandidate:
    selection: str
    probability: float | None
    odds: float | None
    market: str
    line: float | None


def _build_extra_markets(prediction: Prediction) -> ExtraMarketsResponse:
    """Derives double chance and Over/Under goals/corners probabilities from an existing
    Prediction row — see app/models_ml/markets.py for why none of this needs a new model or
    a live recompute (double chance is arithmetic on home/draw/away; totals reuse the stored
    xg_home/xg_away and corners_xg_home/corners_xg_away as a Poisson rate)."""
    home_or_draw, away_or_draw = double_chance_probs(
        prediction.home_prob, prediction.draw_prob, prediction.away_prob
    )
    goals_total = (
        prediction.xg_home + prediction.xg_away
        if prediction.xg_home is not None and prediction.xg_away is not None
        else None
    )
    corners_total = (
        prediction.corners_xg_home + prediction.corners_xg_away
        if prediction.corners_xg_home is not None and prediction.corners_xg_away is not None
        else None
    )
    goals_probs = over_under_probs(goals_total, GOALS_LINES)
    corners_probs = over_under_probs(corners_total, CORNERS_LINES)
    return ExtraMarketsResponse(
        double_chance_home_or_draw_prob=home_or_draw,
        double_chance_away_or_draw_prob=away_or_draw,
        goals_totals=[
            TotalsProbability(line=line, under_prob=under, over_prob=over)
            for line, (under, over) in goals_probs.items()
        ],
        corners_totals=[
            TotalsProbability(line=line, under_prob=under, over_prob=over)
            for line, (under, over) in corners_probs.items()
        ],
    )


def _fixture_query():
    home_team = aliased(Team)
    away_team = aliased(Team)
    return (
        select(
            Fixture,
            Sport.slug,
            League.slug,
            League.name,
            League.country,
            home_team.name,
            away_team.name,
        )
        .join(Sport, Sport.id == Fixture.sport_id)
        .join(League, League.id == Fixture.league_id)
        .join(home_team, home_team.id == Fixture.home_team_id)
        .join(away_team, away_team.id == Fixture.away_team_id)
    )


def _to_summary(
    fixture: Fixture,
    sport_slug: str,
    league_slug: str,
    league_name: str,
    league_country: str | None,
    home_name: str,
    away_name: str,
    best_pick: BestPick | None = None,
    all_market_picks: list[BestPick] | None = None,
    live_state: LiveStateResponse | None = None,
) -> FixtureSummary:
    return FixtureSummary(
        id=fixture.id,
        sport_slug=sport_slug,
        league_slug=league_slug,
        league_name=league_name,
        league_country=league_country,
        home_team=home_name,
        away_team=away_name,
        kickoff_utc=fixture.kickoff_utc,
        status=fixture.status.value,
        season=fixture.season,
        best_pick=best_pick,
        all_market_picks=all_market_picks or [],
        live_state=live_state,
    )


async def _bulk_live_states(db: AsyncSession, fixture_ids: list) -> dict:
    """Bulk-fetch FixtureLiveState for a whole page of /fixtures results — same one-query-not-
    N pattern as _bulk_best_picks, so the Home feed can show a score inline without a
    per-fixture round trip."""
    if not fixture_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(FixtureLiveState).where(FixtureLiveState.fixture_id.in_(fixture_ids))
            )
        )
        .scalars()
        .all()
    )
    return {
        row.fixture_id: LiveStateResponse.model_validate(row, from_attributes=True) for row in rows
    }


def _all_market_candidates(
    prediction: Prediction, odds_by_market: dict[str, list[dict]]
) -> list[_MarketCandidate]:
    """Every real candidate outcome across all four markets for one fixture's prediction —
    h2h (home/draw/away), double chance (1X/X2), Over/Under goals (per GOALS_LINES), Over/Under
    corners (per CORNERS_LINES). Candidates whose probability isn't computable (e.g. no
    draw_prob for NBA, no xg_home/away yet on an older prediction) are simply omitted, never
    fabricated. Odds are looked up per-market from odds_by_market (grouped by DB Odds.market,
    see _bulk_best_picks) — a candidate can have a real probability but no odds yet."""
    candidates: list[_MarketCandidate] = []

    h2h_odds = best_available_odds(odds_by_market.get("h2h", []))
    candidates.append(_MarketCandidate("home", prediction.home_prob, h2h_odds["home"], "h2h", None))
    if prediction.draw_prob is not None:
        candidates.append(
            _MarketCandidate("draw", prediction.draw_prob, h2h_odds["draw"], "h2h", None)
        )
    candidates.append(_MarketCandidate("away", prediction.away_prob, h2h_odds["away"], "h2h", None))

    dc_odds = best_available_odds(odds_by_market.get("double_chance", []))
    home_or_draw, away_or_draw = double_chance_probs(
        prediction.home_prob, prediction.draw_prob, prediction.away_prob
    )
    if home_or_draw is not None:
        candidates.append(
            _MarketCandidate("1X", home_or_draw, dc_odds["home"], "double_chance", None)
        )
    if away_or_draw is not None:
        candidates.append(
            _MarketCandidate("X2", away_or_draw, dc_odds["away"], "double_chance", None)
        )

    goals_total = (
        prediction.xg_home + prediction.xg_away
        if prediction.xg_home is not None and prediction.xg_away is not None
        else None
    )
    for line, (under, over) in over_under_probs(goals_total, GOALS_LINES).items():
        over_odds, under_odds = best_totals_odds(odds_by_market.get("total", []), line)
        if over is not None:
            candidates.append(_MarketCandidate("over", over, over_odds, "goals_total", line))
        if under is not None:
            candidates.append(_MarketCandidate("under", under, under_odds, "goals_total", line))

    corners_total = (
        prediction.corners_xg_home + prediction.corners_xg_away
        if prediction.corners_xg_home is not None and prediction.corners_xg_away is not None
        else None
    )
    for line, (under, over) in over_under_probs(corners_total, CORNERS_LINES).items():
        over_odds, under_odds = best_totals_odds(odds_by_market.get("corners_total", []), line)
        if over is not None:
            candidates.append(_MarketCandidate("over", over, over_odds, "corners_total", line))
        if under is not None:
            candidates.append(_MarketCandidate("under", under, under_odds, "corners_total", line))

    return candidates


def _candidate_to_best_pick(candidate: _MarketCandidate) -> BestPick:
    return BestPick(
        selection=candidate.selection,
        probability=candidate.probability,
        odds=candidate.odds,
        market=candidate.market,
        line=candidate.line,
    )


def _pick_best(candidates: list[_MarketCandidate]) -> BestPick | None:
    """The single highest-probability candidate that has real odds; if NONE of the candidates
    have real odds yet, falls back to the highest-probability candidate overall (probability-
    only, odds=None) — same fallback semantics the old h2h-only version had, just extended
    across every market instead of just three outcomes."""
    with_odds = [c for c in candidates if c.probability is not None and c.odds is not None]
    pool = with_odds or [c for c in candidates if c.probability is not None]
    if not pool:
        return None
    return _candidate_to_best_pick(max(pool, key=lambda c: c.probability))


async def _bulk_best_picks(
    db: AsyncSession, fixture_ids: list, market: str | None = None, line: float | None = None
) -> tuple[dict, dict]:
    """Computes each fixture's single best pick, drawn from ACROSS every market (h2h, double
    chance, Over/Under goals, Over/Under corners) by default — per the user's explicit ask
    that the feed surface "the best odds with the highest probability of winning" regardless
    of which market that lives in, not just home/draw/away. Pass `market` (+`line` for the two
    totals markets) to restrict to one specific market instead (mirrors GET /picks's own
    market/line params). One bulk query each for odds and predictions rather than a
    per-fixture round trip. Unlike /picks, this never filters fixtures out by odds
    threshold on its own — every fixture that has a real prediction gets a best_pick entry,
    odds or not; GET /fixtures's own min_probability/min_odds params do that filtering.

    ALSO returns every real candidate across all four markets per fixture (ignoring the
    market/line restriction — that only applies to the single best_pick), so a past fixture
    can show a full win/loss breakdown across every market, not just its single best pick —
    per the user's explicit ask: "I need all markets predicted in the past to still be
    shown... Over and Under, double chances, corners. Everything should be shown."."""
    if not fixture_ids:
        return {}, {}

    odds_rows = (
        (await db.execute(select(Odds).where(Odds.fixture_id.in_(fixture_ids)))).scalars().all()
    )
    # Grouped by the DB's own market value ("h2h"/"double_chance"/"total"/"corners_total") —
    # _all_market_candidates looks up using these same raw values, not the client-facing
    # "goals_total" label (that translation only matters for the `market` query param below).
    odds_by_fixture_market: dict[tuple, list[dict]] = {}
    for o in odds_rows:
        odds_by_fixture_market.setdefault((o.fixture_id, o.market.value), []).append(
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

    best_picks: dict = {}
    all_picks: dict = {}
    for fixture_id, prediction in latest_prediction_by_fixture.items():
        odds_by_market = {
            db_market: odds_by_fixture_market.get((fixture_id, db_market), [])
            for db_market in ("h2h", "double_chance", "total", "corners_total")
        }
        candidates = _all_market_candidates(prediction, odds_by_market)
        all_picks[fixture_id] = [_candidate_to_best_pick(c) for c in candidates]

        if market and market != "all":
            candidates = [
                c for c in candidates if c.market == market and (line is None or c.line == line)
            ]
        best = _pick_best(candidates)
        if best is not None:
            best_picks[fixture_id] = best

    return best_picks, all_picks


@router.get("/fixtures", response_model=list[FixtureSummary])
async def list_fixtures(
    sport_slug: str | None = None,
    league_slug: str | None = None,
    status_filter: FixtureStatus | None = Query(None, alias="status"),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    # market/line/min_probability/min_odds are all optional and default to "no filtering,
    # combined-best-pick" — existing callers (e.g. the Live tab's status="live" query) that
    # omit them keep getting every fixture with an unfiltered best_pick, exactly as before.
    # The Picks feed is the one caller that opts into all four to get "only fixtures whose
    # best pick — drawn from every market — clears both a probability and an odds floor".
    market: str | None = Query(None, pattern="^(all|h2h|double_chance|goals_total|corners_total)$"),
    line: float | None = Query(None),
    min_probability: float | None = Query(None, ge=0.0, le=1.0),
    min_odds: float | None = Query(None, ge=1.0, le=1000.0),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    if market in _VALID_LINES_BY_MARKET:
        valid_lines = _VALID_LINES_BY_MARKET[market]
        if line is None:
            raise HTTPException(422, detail=f"market={market!r} requires a `line` query param")
        if line not in valid_lines:
            raise HTTPException(
                422, detail=f"market={market!r} only supports line in {valid_lines}"
            )
    elif line is not None:
        raise HTTPException(422, detail=f"`line` is not applicable to market={market!r}")

    stmt = _fixture_query()
    if sport_slug:
        stmt = stmt.where(Sport.slug == sport_slug)
    if league_slug:
        stmt = stmt.where(League.slug == league_slug)
    if status_filter:
        stmt = stmt.where(Fixture.status == status_filter)
    if date_from:
        stmt = stmt.where(Fixture.kickoff_utc >= date_from)
    if date_to:
        stmt = stmt.where(Fixture.kickoff_utc <= date_to)
    stmt = stmt.order_by(Fixture.kickoff_utc).limit(limit).offset(offset)

    rows = (await db.execute(stmt)).all()
    fixture_ids = [row[0].id for row in rows]
    best_picks, all_picks = await _bulk_best_picks(db, fixture_ids, market=market, line=line)
    live_states = await _bulk_live_states(db, fixture_ids)

    # A POSTPONED fixture never gets a best_pick/all_market_picks, regardless of whatever
    # Prediction row might already exist for it — showing "the model would have picked X" for
    # a game that isn't being played is exactly the misleading display the user reported
    # ("the postponed should be displayed in place of the original market prediction/
    # percentage/odd"). The mobile client renders a plain POSTPONED badge off `status` alone.
    summaries = [
        _to_summary(
            *row,
            best_pick=(
                best_picks.get(row[0].id) if row[0].status != FixtureStatus.POSTPONED else None
            ),
            all_market_picks=(
                all_picks.get(row[0].id) if row[0].status != FixtureStatus.POSTPONED else None
            ),
            live_state=live_states.get(row[0].id),
        )
        for row in rows
    ]
    if min_probability is None and min_odds is None:
        return summaries

    # A fixture with no qualifying pick at all (no best_pick, or one that doesn't clear the
    # floor(s)) is dropped from the response entirely — the user's own words: "the intention
    # is not to surface all the games... we just want the best odds with the highest
    # probability of winning." min_odds only applies to fixtures with REAL odds on their best
    # pick — a probability-only pick can never clear an odds floor, by design (never fabricate
    # a price), so it's excluded too when min_odds is set.
    #
    # COMPLETED fixtures are a real exception to all of this: min_probability/min_odds encode
    # "is this worth betting on", a question that only makes sense for a game that hasn't been
    # decided yet. A finished game is being reviewed for how the model actually performed, not
    # considered as a bet — filtering those out by the same threshold hid every past result
    # from the feed entirely (the user's own report: "Results of past games are missing").
    # POSTPONED fixtures are the same kind of exception, for the same reason plus one more:
    # they have no best_pick at all now (see above), so the ordinary "pick is None -> drop it"
    # branch below would silently hide them too — exactly the bug the user reported.
    filtered = []
    for summary in summaries:
        if summary.status in ("completed", "postponed"):
            filtered.append(summary)
            continue
        pick = summary.best_pick
        if pick is None:
            continue
        if min_probability is not None and pick.probability < min_probability:
            continue
        if min_odds is not None and (pick.odds is None or pick.odds < min_odds):
            continue
        filtered.append(summary)
    return filtered


async def _load_fixture_or_404(fixture_id: uuid.UUID, db: AsyncSession):
    stmt = _fixture_query().where(Fixture.id == fixture_id)
    row = (await db.execute(stmt)).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fixture not found")
    return row


@router.get("/fixtures/{fixture_id}", response_model=FixtureDetail)
async def get_fixture(fixture_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    fixture, sport_slug, league_slug, league_name, league_country, home_name, away_name = (
        await _load_fixture_or_404(fixture_id, db)
    )

    live_state_row = (
        await db.execute(select(FixtureLiveState).where(FixtureLiveState.fixture_id == fixture_id))
    ).scalar_one_or_none()

    odds_rows = (
        (await db.execute(select(Odds).where(Odds.fixture_id == fixture_id))).scalars().all()
    )

    prediction_rows = (
        (await db.execute(select(Prediction).where(Prediction.fixture_id == fixture_id)))
        .scalars()
        .all()
    )
    latest_prediction = max(prediction_rows, key=lambda p: p.created_at, default=None)

    home_features = (
        await db.execute(
            select(TeamFeatures).where(
                TeamFeatures.fixture_id == fixture_id, TeamFeatures.team_id == fixture.home_team_id
            )
        )
    ).scalar_one_or_none()
    away_features = (
        await db.execute(
            select(TeamFeatures).where(
                TeamFeatures.fixture_id == fixture_id, TeamFeatures.team_id == fixture.away_team_id
            )
        )
    ).scalar_one_or_none()

    return FixtureDetail(
        **_to_summary(
            fixture,
            sport_slug,
            league_slug,
            league_name,
            league_country,
            home_name,
            away_name,
            live_state=(
                LiveStateResponse.model_validate(live_state_row, from_attributes=True)
                if live_state_row
                else None
            ),
        ).model_dump(),
        odds=[OddsLineResponse.model_validate(o, from_attributes=True) for o in odds_rows],
        # Same suppression as list_fixtures's best_pick/all_market_picks: a POSTPONED fixture
        # keeps whatever Prediction row was written before the postponement was known, but
        # showing it here would be exactly the misleading "original market prediction" the
        # user reported still being displayed for a game that isn't being played.
        prediction=(
            PredictionResponse(
                model_version=latest_prediction.model_version,
                home_prob=latest_prediction.home_prob,
                draw_prob=latest_prediction.draw_prob,
                away_prob=latest_prediction.away_prob,
                confidence_tier=latest_prediction.confidence_tier.value,
                expected_value=latest_prediction.expected_value,
                extra_markets=_build_extra_markets(latest_prediction),
            )
            if latest_prediction and fixture.status != FixtureStatus.POSTPONED
            else None
        ),
        home_team_form=(
            TeamFeaturesResponse.model_validate(home_features, from_attributes=True)
            if home_features
            else None
        ),
        away_team_form=(
            TeamFeaturesResponse.model_validate(away_features, from_attributes=True)
            if away_features
            else None
        ),
    )


@router.get("/fixtures/{fixture_id}/live", response_model=LiveStateResponse)
async def get_fixture_live(fixture_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    live_state = (
        await db.execute(select(FixtureLiveState).where(FixtureLiveState.fixture_id == fixture_id))
    ).scalar_one_or_none()
    if live_state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fixture is not live")
    return LiveStateResponse.model_validate(live_state, from_attributes=True)


@router.get("/fixtures/{fixture_id}/odds", response_model=list[OddsLineResponse])
async def get_fixture_odds(fixture_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    odds_rows = (
        (await db.execute(select(Odds).where(Odds.fixture_id == fixture_id))).scalars().all()
    )
    return [OddsLineResponse.model_validate(o, from_attributes=True) for o in odds_rows]


@router.get("/fixtures/{fixture_id}/prediction", response_model=PredictionResponse)
async def get_fixture_prediction(fixture_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    prediction_rows = (
        (await db.execute(select(Prediction).where(Prediction.fixture_id == fixture_id)))
        .scalars()
        .all()
    )
    latest = max(prediction_rows, key=lambda p: p.created_at, default=None)
    if latest is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No prediction available")
    return PredictionResponse(
        model_version=latest.model_version,
        home_prob=latest.home_prob,
        draw_prob=latest.draw_prob,
        away_prob=latest.away_prob,
        confidence_tier=latest.confidence_tier.value,
        expected_value=latest.expected_value,
        extra_markets=_build_extra_markets(latest),
    )
