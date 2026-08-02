import uuid
from dataclasses import dataclass, replace
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
    HeadToHeadResponse,
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
    # Carried through from the Prediction row so the client can tell a well-informed
    # probability from one the model effectively fell back to the base rate for.
    feature_completeness: float | None = None


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
        tournament_name=fixture.tournament_name,
        tournament_surface=fixture.tournament_surface,
        tournament_location=fixture.tournament_location,
        kickoff_is_estimated=fixture.kickoff_is_estimated,
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

    # Stamped once here rather than threaded through every construction above: completeness is
    # a property of the PREDICTION, identical for every candidate derived from it.
    return [
        replace(candidate, feature_completeness=prediction.feature_completeness)
        for candidate in candidates
    ]


def _candidate_to_best_pick(candidate: _MarketCandidate) -> BestPick:
    return BestPick(
        selection=candidate.selection,
        probability=candidate.probability,
        odds=candidate.odds,
        market=candidate.market,
        line=candidate.line,
        feature_completeness=candidate.feature_completeness,
    )


# How far the model's probability may exceed the bookmaker's implied probability before the
# pick is treated as model error rather than edge. Real betting edges are small (low single
# digits); a double-digit disagreement with a liquid market is overwhelmingly more likely to be
# a miscalibrated model than free money. Chosen against real measured data, not intuition: the
# feed was surfacing Over/Under picks claiming ~85% at odds of 1.60 (≈60% implied after vig) —
# a ~25-point disagreement — and those picks were measured delivering only ~70%. 0.15 is
# deliberately loose enough to keep genuine value while removing the egregious cases.
MAX_EDGE_OVER_MARKET = 0.40
# That bound was calibrated against MEASURED Over/Under overconfidence, and applying it
# unchanged to the 3-way markets proved too strict. A totals market has two outcomes and a
# tight price, so a double-digit disagreement there really is a red flag. A 1X2 market has
# three, and a genuine underdog call legitimately diverges far from the price: in a real case
# (Shenyang Urban 3-1 Shanghai Shenhua) the model said home 57% against a 3.90 price implying
# 25.6% — a 31-point gap — and was RIGHT. Under one shared bound that fixture surfaced no pick
# at all, because every 1X2 and double-chance candidate was rejected while the only survivor
# sat below its own base rate.
#
# So the strict bound stays where it was earned, and a looser one applies where three-way
# uncertainty makes a large disagreement ordinary rather than suspicious.
# Loosened to a sanity backstop rather than an active filter. The tight 0.15 bound was
# introduced to suppress overconfident Over/Under picks, but the base-rate gate
# (MARKET_BASE_RATES) does that job better and more precisely: it removes picks that say
# NOTHING, instead of penalising picks merely for being confident. Keeping both meant the
# guard rejected genuinely winning picks - in a real case (Qingdao 2-0, i.e. under 3.5) the
# 85% under-3.5 call was thrown out for beating the price by 20 points, and it would have won.
#
# What survives here is only the truly absurd: a claim so far from the market that it almost
# certainly reflects stale odds or a broken feature vector rather than a view.
MAX_EDGE_BY_MARKET: dict[str, float] = {}


def _max_edge_for(market: str) -> float:
    return MAX_EDGE_BY_MARKET.get(market, MAX_EDGE_OVER_MARKET)


# What each market returns "for free", measured across all 8,718 real pooled fixtures
# (EPL/Brasileirao/MLS/CSL/Scottish Premiership). These are empirical, not assumed.
#
# They exist because an ABSOLUTE probability floor structurally selects for the LEAST
# informative market. Over/Under probabilities sit naturally near their base rate - "under 3.5
# at 68%" clears a 60% floor arithmetically while carrying no information at all, since 69.4%
# of real fixtures finish under 3.5 anyway. Meanwhile a genuine 57% home call, which is 11
# points above football's real home-win rate, fails the same floor. Measured across every
# prediction, no h2h probability ever reached 0.60 at all.
#
# The real case that exposed this: Shenyang Urban 3-1 Shanghai Shenhua. The model called home
# at 57% and was RIGHT, but that pick failed the 60% floor, leaving only "under 3.5 at 68%" -
# which is below its own base rate, and lost.
MARKET_BASE_RATES: dict[tuple[str, str, float | None], float] = {
    ("h2h", "home", None): 0.4582,
    ("h2h", "draw", None): 0.2538,
    ("h2h", "away", None): 0.2879,
    ("double_chance", "1X", None): 0.7121,
    ("double_chance", "X2", None): 0.5418,
    ("goals_total", "under", 1.5): 0.2338,
    ("goals_total", "over", 1.5): 0.7662,
    ("goals_total", "under", 2.5): 0.4641,
    ("goals_total", "over", 2.5): 0.5359,
    ("goals_total", "under", 3.5): 0.6941,
    ("goals_total", "over", 3.5): 0.3059,
    ("corners_total", "under", 9.5): 0.4545,
    ("corners_total", "over", 9.5): 0.5455,
}

# How far above its market's base rate a pick must sit to count as saying anything. A pick at
# or below base rate is not a prediction, it is the league average wearing a percentage sign.
MIN_EDGE_OVER_BASE_RATE = 0.05


def _base_rate(candidate: _MarketCandidate) -> float | None:
    """The share of real fixtures this outcome occurs in regardless of who is playing.

    None for a market/line with no measured base rate (a line we have not quantified). Such a
    candidate is not filtered out - we cannot judge its informativeness, and silently dropping
    it would be worse than admitting we do not know."""
    return MARKET_BASE_RATES.get((candidate.market, candidate.selection, candidate.line))


def _edge_over_base_rate(candidate: _MarketCandidate) -> float | None:
    """How much this pick beats its own market's base rate by - i.e. how much the model is
    actually telling us about THIS fixture, rather than about the sport in general."""
    base = _base_rate(candidate)
    if base is None or candidate.probability is None:
        return None
    return candidate.probability - base


def _implied_probability(odds: float) -> float:
    """The bookmaker's implied probability, before removing vig. Deliberately NOT vig-adjusted:
    doing so needs every outcome's price for that market, which isn't always ingested, and the
    un-adjusted figure is the CONSERVATIVE direction here — it overstates the book's implied
    probability slightly, so the measured edge comes out slightly smaller and the guard errs
    toward keeping a pick rather than dropping it."""
    return 1.0 / odds


def _expected_value(candidate: _MarketCandidate) -> float:
    """Profit per 1 unit staked at these odds: p*(odds-1) - (1-p), i.e. p*odds - 1."""
    return candidate.probability * candidate.odds - 1.0


def _pick_best(
    candidates: list[_MarketCandidate], min_probability: float | None = None
) -> BestPick | None:
    """The best VALUE pick, not the most likely statement.

    Previously this returned the single highest-PROBABILITY candidate, which had two real,
    user-reported consequences:

      - The feed filled with near-identical "UNDER 3.5" picks (13 of 14 MLS cards in one
        screenshot). Under 3.5 goals is intrinsically an ~80% event, so on raw probability it
        beats any 1X2 (~50%) or double chance (~70%) candidate almost every time. That ranks
        by "what is most likely to be true", which is not the same question as "what is worth
        backing" — an 85% pick at 1.60 can be worse value than a 57% pick at 3.20.
      - Away/X2 picks were effectively invisible. Measured: on the 15 fixtures where the model
        genuinely favoured the away side, X2 surfaced 9/9 times in Brasileirão but 0/6 in
        MLS/CSL, because those leagues' inflated Over/Under probabilities outranked everything.

    Ranking by expected value fixes both, and the edge guard (see MAX_EDGE_OVER_MARKET) removes
    picks whose probability implausibly exceeds the market's — which matters because EV ranking
    alone would still reward an overconfident probability. The two work together: EV decides
    ordering, the guard decides trustworthiness.

    Candidates with no odds can be neither valued nor guarded, so they're ranked by probability
    as before — the only option for a sport with no odds coverage yet (tennis). Odds-bearing
    candidates are always preferred when any survive the guard.

    min_probability is applied HERE, before ranking, rather than to the winner afterwards.
    Ranking globally by EV and only then testing the floor silently discarded whole fixtures:
    the highest-EV candidate is often a high-odds/low-probability one (a real case from the
    test suite: corners OVER at 28% priced 3.50 beats corners UNDER at 72% priced 1.30 on EV),
    so the fixture's best pick would fail a 60% floor even though a perfectly good 72%
    candidate existed. Filtering first answers the question the user is actually asking —
    "the best VALUE among picks at least this likely" — instead of "the best value overall,
    then hide it if it isn't likely enough".

    That floor is now applied RELATIVE TO EACH MARKET'S BASE RATE rather than as one absolute
    number, because an absolute floor structurally selects for the least informative market.
    See MARKET_BASE_RATES: "under 3.5 at 68%" clears a 60% floor while sitting BELOW its own
    69.4% base rate, so it says nothing; a 57% home call sits 11 points above football's real
    home-win rate and says quite a lot, yet fails the same floor. Measured over every stored
    prediction, no h2h probability ever reached 0.60 at all, so an absolute 60% floor silently
    excluded the entire 1X2 market.

    A candidate whose market has no measured base rate is NOT dropped — we can't judge its
    informativeness, and discarding it silently would be worse than admitting that."""
    # min_probability is an ABSOLUTE floor on the displayed probability, because that is what
    # the UI promises. An earlier version made it drive the informativeness requirement
    # instead, and the result was indefensible from the user's side: at a "75%" setting a pick
    # displaying 85% was hidden (only +0.156 over its 0.694 base rate) while one displaying 80%
    # was kept (+0.258 over its 0.542 base rate). A control labelled "minimum probability" must
    # filter on the number shown next to it, or it is lying.
    #
    # Informativeness is enforced SEPARATELY, as a fixed quality bar rather than something the
    # user dials. The two answer different questions - "how likely do you want it" versus "does
    # this pick say anything at all" - and conflating them made both incomprehensible.
    candidates = [
        c
        for c in candidates
        if c.probability is not None
        and (min_probability is None or c.probability >= min_probability)
        # A pick at or below its market's base rate is the league average with a percentage
        # sign on it, not a prediction about THIS fixture.
        and ((edge := _edge_over_base_rate(c)) is None or edge >= MIN_EDGE_OVER_BASE_RATE)
    ]
    priced = [c for c in candidates if c.probability is not None and c.odds is not None]
    unpriced = [c for c in candidates if c.probability is not None and c.odds is None]

    trustworthy = [
        c for c in priced if c.probability - _implied_probability(c.odds) <= _max_edge_for(c.market)
    ]
    if trustworthy:
        return _candidate_to_best_pick(max(trustworthy, key=lambda c: c.probability))
    # Every priced candidate disagreed implausibly with the market. Rather than fall back to
    # the very picks the guard just rejected, fall back only to unpriced ones — and if there
    # are none, return None so the caller drops the fixture entirely. "We have no pick we
    # trust here" is a more honest answer than a confident-looking pick we've just measured
    # as untrustworthy.
    if unpriced:
        return _candidate_to_best_pick(max(unpriced, key=lambda c: c.probability))
    return None


async def _bulk_best_picks(
    db: AsyncSession,
    fixture_ids: list,
    market: str | None = None,
    line: float | None = None,
    min_probability: float | None = None,
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
        best = _pick_best(candidates, min_probability=min_probability)
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
    best_picks, all_picks = await _bulk_best_picks(
        db, fixture_ids, market=market, line=line, min_probability=min_probability
    )
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
    # probability of winning."
    #
    # min_probability applies to COMPLETED fixtures too, per an explicit later user request
    # ("the probability and odd slider does not apply to the tennis records. Make it apply").
    # This deliberately narrows an earlier, broader exemption that had let every completed
    # fixture bypass both floors: that exemption was itself added in response to "Results of
    # past games are missing", but it went too far — reviewing past performance is more useful
    # when it's scoped to the picks you'd actually have taken at your own confidence bar,
    # rather than mixing in ones you'd have filtered out anyway.
    #
    # min_odds deliberately does NOT reject a pick that has no odds at all. An odds floor is
    # unanswerable for a sport with no odds coverage yet (tennis — BallDontLie's tennis /odds
    # is GOAT-tier gated, see app/adapters/factory.py), and the previous "no odds -> fails the
    # floor" rule made every upcoming tennis fixture silently invisible the moment the odds
    # slider moved off its minimum. Filtering on a price we don't have would hide real picks
    # rather than inform anyone; a real price, once ingested, is still filtered normally. The
    # narrow cost is that a football pick with genuinely no ingested price is no longer
    # excluded by an odds floor — rare in practice, since football odds coverage is real.
    #
    # POSTPONED fixtures stay fully exempt: they have no best_pick at all (the backend nulls it
    # out so a pre-postponement prediction can't be shown as if the game were still on), so
    # every branch below would drop them — exactly the bug the user reported.
    filtered = []
    for summary in summaries:
        if summary.status == "postponed":
            filtered.append(summary)
            continue
        pick = summary.best_pick
        if pick is None:
            continue
        if min_probability is not None and pick.probability < min_probability:
            continue
        if min_odds is not None and pick.odds is not None and pick.odds < min_odds:
            continue
        filtered.append(summary)
    return filtered


async def _fetch_head_to_head(
    db: AsyncSession, sport_slug: str, fixture: Fixture
) -> HeadToHeadResponse | None:
    """Real head-to-head history for the fixture detail screen's H2H panel — replaces the raw
    bookmaker-odds table per direct user request. Fetched live, at request time (not persisted,
    not precomputed at ingest) since this is a detail-screen-only concern, not something the
    Picks list needs to filter/sort on — a per-viewed-fixture cost, not a per-ingested-fixture
    one. Football only for now: API-Football has a dedicated /fixtures/headtohead endpoint;
    NBA's own H2H (BallDontLie, via a manual fixture-history search) is real but not wired up
    here — None, not a fabricated empty record, for NBA or any team missing an external_id."""
    if sport_slug != "football":
        return None

    home_team = (
        await db.execute(select(Team).where(Team.id == fixture.home_team_id))
    ).scalar_one_or_none()
    away_team = (
        await db.execute(select(Team).where(Team.id == fixture.away_team_id))
    ).scalar_one_or_none()
    if not home_team or not away_team or not home_team.external_id or not away_team.external_id:
        return None

    from app.adapters.api_football import fetch_h2h_detail

    detail = await fetch_h2h_detail(home_team.external_id, away_team.external_id)
    if detail is None:
        return None

    return HeadToHeadResponse(
        meetings_count=detail.meetings_count,
        home_wins=detail.home_wins,
        draws=detail.draws,
        away_wins=detail.away_wins,
        avg_goals_home=detail.avg_goals_home,
        avg_goals_away=detail.avg_goals_away,
        avg_corners_home=detail.avg_corners_home,
        avg_corners_away=detail.avg_corners_away,
        avg_shots_home=detail.avg_shots_home,
        avg_shots_away=detail.avg_shots_away,
        avg_shots_on_goal_home=detail.avg_shots_on_goal_home,
        avg_shots_on_goal_away=detail.avg_shots_on_goal_away,
        avg_possession_home=detail.avg_possession_home,
        avg_possession_away=detail.avg_possession_away,
    )


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

    head_to_head = await _fetch_head_to_head(db, sport_slug, fixture)

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
        head_to_head=head_to_head,
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
