import dataclasses
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.database import get_db
from app.fixtures.match_stats_cache import get_cached_match_stats, set_cached_match_stats
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team, TeamFeatures
from app.fixtures.schemas import (
    BestPick,
    ComparisonStat,
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
from app.models_ml.corners_reference import blend_probability, bulk_corners_reference
from app.models_ml.markets import CORNERS_LINES, GOALS_LINES, double_chance_probs, over_under_probs
from app.odds.models import Odds
from app.picks.service import (
    best_available_odds,
    best_totals_odds,
    latest_price_per_bookmaker,
)
from app.predictions.models import Prediction, PredictionKind
from app.sports.models import League, Sport

logger = logging.getLogger(__name__)

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


def _build_extra_markets(
    prediction: Prediction, reference_corners: float | None = None
) -> ExtraMarketsResponse:
    """Derives double chance and Over/Under goals/corners probabilities from an existing
    Prediction row — see app/models_ml/markets.py for why none of this needs a new model or
    a live recompute (double chance is arithmetic on home/draw/away; totals reuse the stored
    xg_home/xg_away and corners_xg_home/corners_xg_away as a Poisson rate).

    Corners take the same historical blend the feed's picks do, so the detail screen cannot
    quote a different number from the card that led the user to it."""
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
    reference_probs = over_under_probs(reference_corners, CORNERS_LINES)
    corners_probs = {
        line: (
            blend_probability(under, reference_probs.get(line, (None, None))[0]),
            blend_probability(over, reference_probs.get(line, (None, None))[1]),
        )
        for line, (under, over) in over_under_probs(corners_total, CORNERS_LINES).items()
    }
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


def _newest_odds_per_book(rows: list) -> list:
    """One current row per bookmaker/market/line, for the ORM objects the odds endpoints return.

    Same reason as latest_price_per_bookmaker, which does this for the dict form used by pick
    selection: Odds rows are append-only snapshots, so a fixture accumulates 8.7 per bookmaker
    on average and up to 47. Returning them all makes the odds list a price history rendered as
    if every line were separately available.

    Keyed case-insensitively, because TheRundown writes "draftkings" and API-Football
    "Draftkings" for the same book."""
    newest: dict[tuple, object] = {}
    for row in rows:
        key = ((row.bookmaker or "").strip().lower(), row.market, row.line)
        seen = newest.get(key)
        if seen is None or (
            row.updated_at is not None
            and (seen.updated_at is None or row.updated_at > seen.updated_at)
        ):
            newest[key] = row
    return list(newest.values())


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
    prediction: Prediction,
    odds_by_market: dict[str, list[dict]],
    reference_corners: float | None = None,
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
    # The corners model alone measured WORSE than always backing the more common side (52.1%
    # vs 59.4% at the 10.5 line, on gated picks over 1,277 held-out fixtures), so its
    # probabilities are blended toward a rolling attack/defence reference before they can
    # become a pick. See app/models_ml/corners_reference.py for the measurement and for why
    # head-to-head, the intuitive choice, is deliberately not the reference.
    reference_probs = over_under_probs(reference_corners, CORNERS_LINES)
    for line, (under, over) in over_under_probs(corners_total, CORNERS_LINES).items():
        ref_under, ref_over = reference_probs.get(line, (None, None))
        under = blend_probability(under, ref_under)
        over = blend_probability(over, ref_over)
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
    ("goals_total", "under", 4.5): 0.8481,
    ("goals_total", "over", 4.5): 0.1519,
    ("corners_total", "under", 9.5): 0.4545,
    ("corners_total", "over", 9.5): 0.5455,
    ("corners_total", "under", 10.5): 0.5703,
    ("corners_total", "over", 10.5): 0.4297,
}

# Base rates are PER SPORT. The table above is football's, measured on football fixtures, and
# applying it to another sport silently changes what "informative" means.
#
# Tennis is the case that forced this. "Home" there is not a venue — tennis has no home court —
# it is an arbitrary but stable tiebreak (lower external player id, see
# balldontlie_tennis.py:_home_away_players). That tiebreak is not 50/50: measured over 17,434
# real completed ATP matches (2021-2025), the lower-id player wins 62.17%, and 61.77% across
# all 64,245 matches back to 2007. Ids correlate loosely with how long a player has been on
# tour, so the lower id is usually the more established player.
#
# Judged against football's 45.82%, a tennis "home 55%" call looked like +9 points of real
# information when it is actually 7 points BELOW what the tiebreak alone gives you. The gate
# was admitting picks that say less than nothing.
#
# Measured with ml/data/tennis_match_stats_atp.parquet; cross-checked against the independent
# training game log, which agrees to four decimals (0.6217). A first attempt disagreed
# (0.5198) purely because PLAYER_ID is stored as a string, so min() compared "10" < "9"
# lexicographically — worth knowing before re-deriving these.
_TENNIS_BASE_RATES: dict[tuple[str, str, float | None], float] = {
    # DELIBERATELY EMPTY. Tennis abstains from the base-rate gate entirely, for the same reason
    # it already abstained for draw/double-chance/goals/corners: those keys are absent rather
    # than zeroed, so _base_rate returns None and the gate declines to judge a market this sport
    # does not have. HOME/AWAY IS SUCH A MARKET.
    #
    # It used to hold ("h2h","home"): 0.6217 and ("h2h","away"): 0.3783. Those numbers were real
    # — the player labelled "home" does win about that often — but "home" in tennis is not a
    # venue. app/adapters/balldontlie_tennis.py:_home_away_players assigns home = the LOWER
    # BallDontLie player id, purely so a fixture's sides cannot flip between an early scheduled
    # ingest and a later completed one. Measured 2026-08-13, that ordering is a weak proxy for
    # strength: corr(player_id, rank_points) = -0.11, and the lower-id player is the
    # higher-ranked one 69% of the time. So the base rate encoded "the stronger player usually
    # wins" wearing a home/away label.
    #
    # Two things followed, and the second is why this is now empty rather than merely refreshed:
    #   1. DOUBLE COUNTING. rank_diff is the model's primary feature, so its probability for the
    #      home player ALREADY prices in that he is the stronger one. Requiring him to clear a
    #      prior that exists BECAUSE he is the stronger one charges the same fact twice.
    #   2. THE BARS CANNOT BE SYMMETRIC. Two base rates summing to 1, each plus 5pp, put the bar
    #      at 0.672 for home and 0.428 for away — symmetric around the base-rate split, not
    #      around 0.5. Against a model whose probabilities cluster in 0.44-0.69 the away slot
    #      almost always clears and the home slot almost never does. Measured over all 669
    #      tennis predictions: 167 (25.0%) were INVERTED — the model favoured home, home failed
    #      its bar, away cleared it, so the product recommended the player the model rated LOWER.
    #
    # The gate is also redundant here. "Is this confident enough to show?" is already answered by
    # the user's own min_probability slider (default 0.6), by MIN_FEATURE_COMPLETENESS, and by
    # MAX_EDGE_OVER_MARKET — all in coordinates a user can see. This was a fourth, hidden filter
    # keyed on our own row ordering.
    #
    # WHAT WOULD BRING A GATE BACK, pre-registered before the data exists: a tennis pick should
    # have to beat the strategy a user could actually run — "back the higher-ranked player",
    # which hits 0.6296 over 17,480 real matches (Hard 0.6325 / Clay 0.6202 / Grass 0.6377, a
    # 1.75pp spread, so it is not surface-dependent in any way that matters). train_tennis.py
    # now reports the model against exactly that, pooled and per surface. Reintroduce a gate
    # only if the model beats that baseline by >= 3pp on the test split AND the per-surface
    # figures do not show the edge coming from one surface alone. The current margin is 1.6pp
    # (63.86% vs 62.22%), which is why the honest response today is no gate rather than a gate
    # against an artifact.
}

BASE_RATES_BY_SPORT: dict[str, dict[tuple[str, str, float | None], float]] = {
    "tennis": _TENNIS_BASE_RATES,
}

# How far above its market's base rate a pick must sit to count as saying anything. A pick at
# or below base rate is not a prediction, it is the league average wearing a percentage sign.
MIN_EDGE_OVER_BASE_RATE = 0.05

# How much of the model's own feature vector must have had a real value for its output to be
# offered as a pick. Below this the number is not a prediction about the fixture; it is the
# model's fallback prior with a team name attached.
#
# The motivating case: Tottenham vs Newcastle, 2026-08-29, with 3 of 31 features populated
# (EPL's season had not started, so neither side had played a match). It served 1X at 99.7% --
# Newcastle to neither win nor draw at 0.3%. Running the same near-empty vector through the
# previous 9-league artefact gave 93.6%, so this predates the 18-league pool and pooling made
# an existing defect worse rather than creating it.
#
# MEASURED, not chosen by feel, over all 159 football predictions carrying a completeness
# value (fraction of picks whose best 1X/X2 probability was extreme):
#
#     completeness    n    >=0.90    settled accuracy
#     0.00-0.15      26    22 (85%)   0% (n=1)
#     0.15-0.25       8     1 (13%)  43% (n=7)
#     0.25-0.35      17     1  (6%)  33% (n=3)
#     0.35-0.50      44     0  (0%)  38% (n=13)
#     0.50+          64    62 (97%)  81% (n=64)
#
# The 0.50+ row is why this is a completeness floor and NOT a cap on extreme probabilities:
# that band is just as extreme and is 81% correct on a real settled sample. Confident output
# built on real data is the product working. Capping by probability would blunt exactly the
# band that earns its confidence while leaving the empty-vector band untouched.
#
# 0.25 was where the EXTREMENESS cliff is -- 85% extreme below it, 6% just above -- and that
# reading still stands. It was set deliberately BELOW mobile's LOW_CONFIDENCE_COMPLETENESS of
# 0.35 on the reasoning that the two are different instruments: 0.35 dims a badge, a soft signal
# that costs nothing if over-applied, while this removes a pick outright.
#
# RAISED TO 0.35 on 2026-08-13, and NOT on the strength of the accuracy numbers. Stating that
# plainly because the accuracy numbers were the stated reason before they were looked at
# properly, and they do not support it -- every band interval overlaps every other:
#
#     football, settled, one row per fixture
#     0.00-0.25   n= 9   acc 0.333   95% CI [0.12, 0.65]
#     0.25-0.35   n= 5   acc 0.200   95% CI [0.04, 0.62]
#     0.35-0.50   n=13   acc 0.385   95% CI [0.18, 0.64]
#     0.50-0.65   n= 6   acc 0.833   95% CI [0.44, 0.97]
#
# The 0.200 rests on FIVE fixtures; two more correct results would make it 0.60. If anything
# these hint at a cliff nearer 0.50, on n=6, which is thinner still. No floor can be located
# from this, and the earlier claim that two measurements "agree on 0.35" was wrong.
#
# The two reasons that DO hold, neither of them statistical:
#   1. COHERENCE. Mobile dims below 0.35 and captions it "limited data". Between 0.25 and 0.35
#      the product therefore recommended a pick while simultaneously telling the user its data
#      was limited. That is incoherent whatever the accuracy turns out to be, and one of the two
#      numbers had to move; the safer direction is the one that shows fewer, better-founded
#      picks.
#   2. IT COSTS NOTHING TODAY. Measured at the time of the change: ZERO upcoming picks sit in
#      the 0.25-0.35 band (football 143 upcoming, mean completeness 0.540; tennis 4, mean
#      0.839). It is protection for the next season opening, when EPL and the Scottish
#      Premiership last ran at 0.12-0.19, not a cut to the current feed.
#
# NOT raised to 0.50, though the same weak evidence points there: that would drop 11 real
# upcoming football picks on the strength of a six-fixture band. Removing real picks needs
# better evidence than keeping them.
#
# Checked PER SPORT rather than assumed, because applying a football-derived constant to tennis
# is exactly the error that made the tennis base-rate gate invert a quarter of its picks (see
# _TENNIS_BASE_RATES). Tennis has 3 settled fixtures in the affected band and 0 upcoming, so it
# is neither helped nor harmed.
#
# WHEN TO REVISIT: football currently has 33 settled fixtures carrying a completeness value.
# At 93 -- the MIN_REPORTABLE_N used by /history, the smallest n whose Wilson interval is under
# 20pp wide -- the bands become separable and this should be re-derived rather than re-argued.
#
# NULL passes deliberately. feature_completeness was added without a backfill because older
# predictions genuinely have no measurement -- treating unmeasured as failing would silently
# erase every prediction made before that migration.
MIN_FEATURE_COMPLETENESS = 0.35

# The floor a SETTLED fixture is judged against. This is the value MIN_FEATURE_COMPLETENESS held
# before it was raised on 2026-08-13, and that is exactly why it exists.
#
# The settled exemption was introduced because RAISING the floor reached backwards and deleted
# published results -- the Hearts v Dundee Utd card at 0.32, a corners pick that had been shown
# and had won. But it was implemented as "settled fixtures skip the floor entirely", which
# overshot: it also surfaced picks that never cleared the OLD floor either, and so were
# suppressed the whole time they were live and were never shown to anybody.
#
# Measured in production 2026-08-16, five such cards, the worst of them:
#
#     Chongqing Tongliang Long v SHANGHAI SIPG   away 1.00   completeness 0.23
#
# An away side at 100% off a vector that was 77% missing. ADDING a pick nobody saw is the same
# defect as deleting one they did -- the principle this module already states, applied against
# itself.
#
# So a settled fixture is judged against the floor that applied WHEN IT WAS LIVE, not against no
# floor at all. 0.25 restores everything the pre-raise product actually published while still
# refusing what it never did.
SETTLED_FEATURE_COMPLETENESS_FLOOR = 0.25


def _base_rate(candidate: _MarketCandidate, sport_slug: str | None = None) -> float | None:
    """The share of real fixtures this outcome occurs in regardless of who is playing.

    Sport-specific rates win where they exist; otherwise the football-measured table applies.
    NBA deliberately has no override — its h2h home rate is a real home-court advantage in the
    same 45-55% territory the football numbers describe, so borrowing them is defensible in a
    way borrowing them for tennis was not.

    None for a market/line with no measured base rate (a line we have not quantified, or a
    market the sport does not have). Such a candidate is not filtered out - we cannot judge its
    informativeness, and silently dropping it would be worse than admitting we do not know."""
    key = (candidate.market, candidate.selection, candidate.line)
    if sport_slug is not None:
        by_sport = BASE_RATES_BY_SPORT.get(sport_slug)
        if by_sport is not None:
            return by_sport.get(key)
    return MARKET_BASE_RATES.get(key)


def _edge_over_base_rate(
    candidate: _MarketCandidate, sport_slug: str | None = None
) -> float | None:
    """How much this pick beats its own market's base rate by - i.e. how much the model is
    actually telling us about THIS fixture, rather than about the sport in general."""
    base = _base_rate(candidate, sport_slug)
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


# Markets barred from WINNING the headline pick, because they have been measured to carry no
# information about the specific fixture. This is not a confidence cap and not a base-rate
# guard — both of those judge an individual pick. This judges the MARKET, on whether its
# predicted value correlates with what actually happens.
#
# Measured on real settled fixtures, predicted total vs actual total:
#
#     goals_total     n=242   r=+0.049   0.2% of variance explained   <- barred
#     corners_total   n=234   r=+0.288   8.3% of variance explained   <- kept
#
# Corners is deliberately KEPT. The two were assumed to be the same case and are not: at
# n=234, r=+0.288 sits about 4.4 standard errors from zero, a real if modest signal. Goals is
# indistinguishable from zero and no further calibration can change that — the model's own
# reliability buckets for under 3.5 all land on the base rate, and two independent
# measurements agree. See CLAUDE.md, "Negative Binomial ... DELIBERATELY NOT BUILT".
#
# Goals still appears in all_market_picks and in the fixture detail's Other Markets, so nothing
# is hidden; it simply cannot be the pick we lead with. An explicit market=goals_total request
# is still honoured — asking for it is different from it winning by default.
NO_DEMONSTRATED_SIGNAL_MARKETS = frozenset({"goals_total"})


def _pick_best(
    candidates: list[_MarketCandidate],
    min_probability: float | None = None,
    sport_slug: str | None = None,
    is_settled: bool = False,
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
    informativeness, and discarding it silently would be worse than admitting that.

    is_settled judges a DECIDED fixture against SETTLED_FEATURE_COMPLETENESS_FLOOR — the floor
    that applied while it was live — instead of the current MIN_FEATURE_COMPLETENESS, and
    touches that guard ONLY.

    Reported by a user: a Hearts v Dundee Utd card from 2026-08-09 had shown under-10.5 corners
    at 2.05, the match finished 7-2 on corners so the pick WON, and days later the card carried
    no pick. Nothing about the fixture changed. The floor was raised 0.25 -> 0.35 on 2026-08-13,
    that prediction sits at 0.32, and best_pick is recomputed per request — so a guard tightened
    after the fact reached backwards and deleted a published result.

    THE BIAS IS THE ARGUMENT, not tidiness. Retroactive filtering is not neutral: sub-0.35
    picks measure 0.2857 accuracy against 0.5263 at or above it, so dropping them from history
    makes the visible track record BETTER than the real one. A history that improves every time
    a guard tightens is not a history. 21 picks over 14 days had been erased, 9 of them Scottish
    Premiership.

    SCOPED TO THIS ONE GUARD DELIBERATELY, after trying it the other way. Exempting the others
    was tried first, on the tidier-sounding principle that no bet-worthiness test belongs on a
    played match, and it rewrote history in the opposite direction:
      - lifting NO_DEMONSTRATED_SIGNAL_MARKETS put goals under-4.5 at 1.18 on the reported card
        in place of the corners pick the user actually saw;
      - lifting MIN_EDGE_OVER_BASE_RATE surfaced a 1X pick sitting BELOW its own base rate on a
        fixture that had been correctly hidden all along (caught by an existing test).
    ADDING a pick that was never shown is the same defect as removing one that was. So the
    exemption covers only the guard that actually tightened and erased, and MIN_EDGE_OVER_BASE_
    RATE / MAX_EDGE_OVER_MARKET / the barred-market rule still apply everywhere, unchanged.

    min_probability also still applies: it is the user's own visible control, and a card
    disappearing when they raise their own slider is that control working.

    HONEST LIMIT: this does NOT replay the card as shown, and cannot. Ranking still runs against
    today's odds, so a settled card shows what the model called rather than a reconstruction.
    Only pick_snapshots can do that, and it holds nothing before 2026-08-10 — the reported
    fixture is 08-09, so that particular card is genuinely unrecoverable. Snapshots are the
    long-term answer; this stops the tightening ratchet in the meantime."""
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
        and (
            (edge := _edge_over_base_rate(c, sport_slug)) is None or edge >= MIN_EDGE_OVER_BASE_RATE
        )
        # A vector that was mostly missing at inference time cannot say anything about THIS
        # fixture, however confident the number looks. See MIN_FEATURE_COMPLETENESS.
        #
        # A settled fixture is judged against the floor that applied WHEN IT WAS LIVE rather
        # than exempted outright -- see SETTLED_FEATURE_COMPLETENESS_FLOOR for why the outright
        # exemption overshot and surfaced picks nobody was ever shown.
        and (
            c.feature_completeness is None
            or c.feature_completeness
            >= (SETTLED_FEATURE_COMPLETENESS_FLOOR if is_settled else MIN_FEATURE_COMPLETENESS)
        )
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


def _prediction_precedence(prediction: Prediction) -> tuple:
    """Which of a fixture's predictions the card should show.

    A REAL PRE-KICKOFF FORECAST ALWAYS BEATS A RETRODICTION, however much later the
    retrodiction was written. Picking purely by created_at -- which this did until 2026-08-16 --
    means running the retrodiction backfill silently REWRITES the pick on every past card that
    already had a genuine forecast.

    That is not hypothetical. Reported the day it happened: three WNBA cards from Friday, all
    winners, came back as a different set with a loss among them. Las Vegas Aces v Washington
    Mystics 83-76 had been HOME 0.64 (a win) and became AWAY 0.85 (a loss) -- same fixture, same
    score, a pick the user never saw. Locally that league held 16 PRE_MATCH predictions and 22
    RETRODICTION ones, and the newer rows won.

    Retrodiction exists to fill fixtures that never had a forecast, not to restate ones that
    did. Within the same kind the newest row still wins, which is how a forecast revised as
    injuries and odds landed keeps its final pre-kickoff value.
    """
    return (prediction.kind == PredictionKind.PRE_MATCH, prediction.created_at)


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
                "bookmaker": o.bookmaker,
                "updated_at": o.updated_at,
                "home_odds": o.home_odds,
                "draw_odds": o.draw_odds,
                "away_odds": o.away_odds,
                "line": o.line,
                "over_odds": o.over_odds,
                "under_odds": o.under_odds,
            }
        )
    # Odds are append-only snapshots, so without this the "best" price is a high-water mark
    # over the fixture's whole price history rather than one a user could still take. Measured
    # on 119 real fixtures: 28% overstated, by 5.89% on average. See latest_price_per_bookmaker.
    odds_by_fixture_market = {
        key: latest_price_per_bookmaker(rows) for key, rows in odds_by_fixture_market.items()
    }

    # Which sport each fixture belongs to, so the base-rate gate uses that sport's own rates
    # rather than football's (see BASE_RATES_BY_SPORT — tennis "home" wins 62% of the time,
    # against football's 45.8%, so the wrong table admits picks that say less than nothing).
    # The Fixture rows themselves are needed for the corners reference, which keys off both
    # team ids and the kickoff time, so both come from one query.
    fixture_rows = (
        await db.execute(
            select(Fixture, Sport.slug)
            .join(Sport, Sport.id == Fixture.sport_id)
            .where(Fixture.id.in_(fixture_ids))
        )
    ).all()
    sport_by_fixture: dict = {fixture.id: slug for fixture, slug in fixture_rows}
    # A decided fixture is reviewed, not backed — see _pick_best's is_settled. POSTPONED is not
    # settled: it has no result, and list_fixtures already nulls its best_pick outright.
    settled_fixtures: set = {
        fixture.id for fixture, _ in fixture_rows if fixture.status == FixtureStatus.COMPLETED
    }
    reference_corners = await bulk_corners_reference(db, [fixture for fixture, _ in fixture_rows])

    prediction_rows = (
        (await db.execute(select(Prediction).where(Prediction.fixture_id.in_(fixture_ids))))
        .scalars()
        .all()
    )
    latest_prediction_by_fixture: dict = {}
    for p in prediction_rows:
        existing = latest_prediction_by_fixture.get(p.fixture_id)
        if existing is None or _prediction_precedence(p) > _prediction_precedence(existing):
            latest_prediction_by_fixture[p.fixture_id] = p

    best_picks: dict = {}
    all_picks: dict = {}
    for fixture_id, prediction in latest_prediction_by_fixture.items():
        odds_by_market = {
            db_market: odds_by_fixture_market.get((fixture_id, db_market), [])
            for db_market in ("h2h", "double_chance", "total", "corners_total")
        }
        candidates = _all_market_candidates(
            prediction, odds_by_market, reference_corners.get(fixture_id)
        )
        all_picks[fixture_id] = [_candidate_to_best_pick(c) for c in candidates]

        if market and market != "all":
            candidates = [
                c for c in candidates if c.market == market and (line is None or c.line == line)
            ]
        else:
            # Only when no market was explicitly asked for. A caller naming goals_total wants
            # goals_total; what is barred is it winning the DEFAULT cross-market ranking.
            #
            # DELIBERATELY still applied to settled fixtures, unlike the guards in _pick_best,
            # and this was tried the other way first. Lifting it for settled fixtures put goals
            # under-4.5 at 1.18 on the reported Hearts v Dundee Utd card in place of the
            # corners pick the user actually saw — rewriting the past by ADDING rather than
            # removing. The distinction that matters: the guards in _pick_best DELETE a
            # fixture's pick outright, while this one only decides which market wins the
            # headline, and it decides that the same way on every day at once.
            candidates = [c for c in candidates if c.market not in NO_DEMONSTRATED_SIGNAL_MARKETS]
        best = _pick_best(
            candidates,
            min_probability=min_probability,
            sport_slug=sport_by_fixture.get(fixture_id),
            is_settled=fixture_id in settled_fixtures,
        )
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
    # A fixture the provider WITHDREW is hidden; one it explicitly called off is not. Both are
    # POSTPONED, but they mean different things to a user: a called-off match was real and is
    # worth telling them about, while a withdrawn one was never a scheduled match at all. The
    # case this was built for produced 33 grey cards for a provisional tennis draw that never
    # existed, burying the day's two genuine picks. Detail (`GET /fixtures/{id}`) still serves
    # them, so an existing deep link does not break.
    stmt = stmt.where(Fixture.withdrawn.is_(False))
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
    one.

    All three sports now, each through its own provider, with the DEPTH THE PROVIDER ACTUALLY
    ALLOWS rather than a shape forced to match:

        football   5 rows   API-Football /fixtures/headtohead + /fixtures/statistics
        tennis     6 rows   BallDontLie /head_to_head (record) + /match_stats (serve/return)
        NBA/WNBA   2 rows   BallDontLie /games -- /stats is 401 on this plan, so the final
                            score is the only real per-meeting number that exists

    None, never a fabricated empty record, for two teams that have never met or a team with no
    external_id."""
    home_team = (
        await db.execute(select(Team).where(Team.id == fixture.home_team_id))
    ).scalar_one_or_none()
    away_team = (
        await db.execute(select(Team).where(Team.id == fixture.away_team_id))
    ).scalar_one_or_none()
    if not home_team or not away_team or not home_team.external_id or not away_team.external_id:
        return None

    league_slug = (
        await db.execute(select(League.slug).where(League.id == fixture.league_id))
    ).scalar_one_or_none()

    if sport_slug == "football":
        return await _football_head_to_head(home_team, away_team)
    if sport_slug in ("nba", "tennis"):
        return await _balldontlie_head_to_head(sport_slug, league_slug, home_team, away_team)
    return None


async def _balldontlie_head_to_head(
    sport_slug: str, league_slug: str | None, home_team: Team, away_team: Team
) -> HeadToHeadResponse | None:
    """Basketball and tennis both come from BallDontLie, through different namespaces and very
    different endpoints — hence one branch per sport rather than one shared call."""
    if sport_slug == "tennis":
        from app.adapters.balldontlie_tennis import fetch_h2h_panel

        league = league_slug or "atp"
    else:
        from app.adapters.balldontlie import fetch_h2h_panel

        league = league_slug or "nba"

    try:
        panel = await fetch_h2h_panel(home_team.external_id, away_team.external_id, league)
    except httpx.HTTPError:
        # The panel is an enrichment; a rate limit or an outage must not fail the whole
        # fixture screen. BallDontLie's NBA tier is 5 req/min, so this is a live possibility.
        logger.warning("H2H fetch failed for %s; rendering the fixture without it", sport_slug)
        return None
    if panel is None:
        return None
    return HeadToHeadResponse(
        meetings_count=panel.meetings_count,
        home_wins=panel.home_wins,
        draws=panel.draws,
        away_wins=panel.away_wins,
        stats=[
            ComparisonStat(label=s.label, home=s.home, away=s.away, suffix=s.suffix)
            for s in panel.stats
        ],
    )


async def _football_head_to_head(home_team: Team, away_team: Team) -> HeadToHeadResponse | None:
    from app.adapters.api_football import H2HDetail, fetch_h2h_detail
    from app.core.redis import get_redis
    from app.fixtures.h2h_cache import get_cached_h2h, set_cached_h2h

    # Cached because this panel cost up to SIX live API calls on EVERY view (2-3s measured),
    # for facts that do not change until these two teams next meet. See h2h_cache.py.
    redis = get_redis()
    hit, detail = await get_cached_h2h(
        redis, home_team.external_id, away_team.external_id, H2HDetail
    )
    if not hit:
        try:
            detail = await fetch_h2h_detail(home_team.external_id, away_team.external_id)
        except httpx.HTTPError:
            # Quota exhaustion arrives here as APIFootballQuotaExceeded. The panel is an
            # enrichment; losing it must not fail the whole fixture screen, which is exactly
            # what happened when the daily allowance ran out.
            logger.warning("H2H fetch failed; rendering the fixture without the panel")
            return None
        await set_cached_h2h(redis, home_team.external_id, away_team.external_id, detail)
    if detail is None:
        return None

    # Football's named fields mapped onto the shared row shape. A row is omitted entirely when
    # NEITHER side has a value, rather than rendered as a pair of dashes.
    rows = [
        ("Goals", detail.avg_goals_home, detail.avg_goals_away, ""),
        ("Corners", detail.avg_corners_home, detail.avg_corners_away, ""),
        ("Total shots", detail.avg_shots_home, detail.avg_shots_away, ""),
        ("Shots on goal", detail.avg_shots_on_goal_home, detail.avg_shots_on_goal_away, ""),
        ("Possession", detail.avg_possession_home, detail.avg_possession_away, "%"),
    ]
    return HeadToHeadResponse(
        meetings_count=detail.meetings_count,
        home_wins=detail.home_wins,
        draws=detail.draws,
        away_wins=detail.away_wins,
        # Rounded here so the API is consistent across sports: the basketball and tennis
        # adapters already round, and football was emitting 7.666666666666667 for a mean of
        # three integers. The client rounds for display anyway, so this is about the payload
        # being readable rather than about what a user sees.
        stats=[
            ComparisonStat(
                label=label,
                home=None if home is None else round(home, 1),
                away=None if away is None else round(away, 1),
                suffix=suffix,
            )
            for label, home, away, suffix in rows
            if home is not None or away is not None
        ],
    )


async def _fetch_match_stats(
    db: AsyncSession,
    sport_slug: str,
    league_slug: str | None,
    fixture: Fixture,
    live_state: FixtureLiveState | None,
) -> list[ComparisonStat]:
    """What actually happened in THIS match, so a settled prediction can be read against the
    result rather than taken on trust.

    The card already shows a tick or a cross; this is the evidence behind it. A corners pick
    that settled at 9 and one that settled at 14 both render as one green tick, and only one of
    them was close.

    COMPLETED FIXTURES ONLY, and that gate is load-bearing rather than cosmetic: the rows are
    cached for 30 days on the grounds that a played match's statistics are immutable, which
    stops being true the moment a live fixture is allowed in -- it would freeze a score
    mid-match. See match_stats_cache.py.

        football   4 rows   API-Football /fixtures/statistics, plus goals from our own settled
                            live_state (already stored, so no call is spent on it)
        tennis     6 rows   BallDontLie /match_stats -- the same serve and return measures the
                            H2H panel shows, for this one match
        NBA/WNBA   none     /stats is 401 on this plan, so the final score is the only real
                            per-match number and it is already displayed above the panel

    Returns [] rather than raising for every unhappy path -- an unplayed fixture, a provider
    outage, a competition with no published statistics. The panel is an enrichment; losing it
    must not fail the fixture screen, which is exactly what the H2H panel had to learn when a
    daily quota ran out.
    """
    # Basketball has no per-match statistics at any price we hold, so there is nothing to fetch
    # and nothing worth caching -- returning before the cache also keeps its keyspace to the
    # sports that actually use it.
    if (
        fixture.status != FixtureStatus.COMPLETED
        or not fixture.external_id
        or sport_slug not in ("football", "tennis")
    ):
        return []

    try:
        if sport_slug == "football":
            return await _football_match_stats(db, fixture, live_state)
        return await _tennis_match_stats(db, league_slug, fixture)
    except httpx.HTTPError:
        logger.warning("Match-stats fetch failed; rendering the fixture without the panel")
        return []


async def _cached_provider_payload(sport_slug: str, fixture_external_id: str, fetch):
    """Fetch-through cache for the PROVIDER'S answer about one completed match.

    Deliberately caches the raw provider payload rather than the rendered rows, because the
    rows are not purely remote: goals and corners are read from our own fixture_live_state,
    which keeps changing after the match. `_backfill_corners_from_thestatsapi` fills corner
    counts for up to seven days afterwards, and caching the finished panel would freeze that
    absence for the full 30-day TTL and permanently hide a count that arrived on day three.
    """
    from app.core.redis import get_redis

    redis = get_redis()
    hit, cached = await get_cached_match_stats(redis, sport_slug, fixture_external_id)
    if hit:
        return cached
    payload = await fetch()
    await set_cached_match_stats(redis, sport_slug, fixture_external_id, payload)
    return payload


async def _football_match_stats(
    db: AsyncSession, fixture: Fixture, live_state: FixtureLiveState | None
) -> list[ComparisonStat]:
    from app.adapters.api_football import fetch_match_stats

    async def fetch():
        stats_by_team = await fetch_match_stats(fixture.external_id)
        return {team_id: dataclasses.asdict(s) for team_id, s in stats_by_team.items()}

    stats_by_team = await _cached_provider_payload("football", fixture.external_id, fetch)
    home_team = (
        await db.execute(select(Team).where(Team.id == fixture.home_team_id))
    ).scalar_one_or_none()
    away_team = (
        await db.execute(select(Team).where(Team.id == fixture.away_team_id))
    ).scalar_one_or_none()
    return _football_stat_rows(
        stats_by_team.get(home_team.external_id) if home_team else None,
        stats_by_team.get(away_team.external_id) if away_team else None,
        live_state,
    )


def _football_stat_rows(
    home: dict | None, away: dict | None, live_state: FixtureLiveState | None
) -> list[ComparisonStat]:
    """Pure row assembly, split out from the fetch so it is testable without a provider, a
    database or Redis — the precedence rules below are the part worth pinning."""

    # Goals come from our OWN settled live_state, not from the statistics call: it is already
    # stored, it is what the tick or cross was graded against, and a panel that disagreed with
    # the score printed directly above it would be worse than no panel.
    rows: list[tuple[str, float | None, float | None, str]] = [
        (
            "Goals",
            live_state.home_score if live_state else None,
            live_state.away_score if live_state else None,
            "",
        ),
        (
            "Corners",
            _stat_or_stored(home, "corners", live_state, "home_corners"),
            _stat_or_stored(away, "corners", live_state, "away_corners"),
            "",
        ),
        ("Total shots", _stat(home, "shots"), _stat(away, "shots"), ""),
        ("Shots on goal", _stat(home, "shots_on_goal"), _stat(away, "shots_on_goal"), ""),
        ("Possession", _stat(home, "possession_pct"), _stat(away, "possession_pct"), "%"),
    ]
    return [
        ComparisonStat(label=label, home=home_value, away=away_value, suffix=suffix)
        for label, home_value, away_value, suffix in rows
        if home_value is not None or away_value is not None
    ]


def _stat(stats: dict | None, field: str) -> float | None:
    """stats is the cached provider payload, so a plain dict rather than a MatchStats — the
    cache round-trips through JSON and reviving the dataclass would buy nothing here."""
    value = stats.get(field) if stats else None
    return None if value is None else float(value)


def _stat_or_stored(stats: dict | None, field: str, live_state, stored_field: str) -> float | None:
    """Corners exist in two places and they must not disagree.

    fixture_live_state already holds the real counts, captured at settlement and, where
    API-Football never supplied them, backfilled from TheStatsAPI -- which is the only source
    for whole leagues (Veikkausliiga has 0% API-Football corner coverage). Those stored counts
    are also what graded the corners pick, so they win; the live call fills a gap rather than
    overriding a settled fact."""
    stored = getattr(live_state, stored_field, None) if live_state else None
    if stored is not None:
        return float(stored)
    return _stat(stats, field)


async def _tennis_match_stats(
    db: AsyncSession, league_slug: str | None, fixture: Fixture
) -> list[ComparisonStat]:
    from app.adapters.balldontlie_tennis import fetch_match_stat_panel

    home_team = (
        await db.execute(select(Team).where(Team.id == fixture.home_team_id))
    ).scalar_one_or_none()
    away_team = (
        await db.execute(select(Team).where(Team.id == fixture.away_team_id))
    ).scalar_one_or_none()
    if not home_team or not away_team or not home_team.external_id or not away_team.external_id:
        return []

    async def fetch():
        panel = await fetch_match_stat_panel(
            fixture.external_id,
            home_team.external_id,
            away_team.external_id,
            league_slug or "atp",
        )
        return [dataclasses.asdict(s) for s in panel]

    # Tennis has no local component the way football's goals and corners do -- every row comes
    # from /match_stats -- so here the cached payload IS the rows.
    rows = await _cached_provider_payload("tennis", fixture.external_id, fetch)
    return [ComparisonStat(**row) for row in rows]


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

    odds_rows = _newest_odds_per_book(
        (await db.execute(select(Odds).where(Odds.fixture_id == fixture_id))).scalars().all()
    )

    prediction_rows = (
        (await db.execute(select(Prediction).where(Prediction.fixture_id == fixture_id)))
        .scalars()
        .all()
    )
    # Same precedence the feed uses, so the detail screen cannot disagree with the card it was
    # opened from -- see _prediction_precedence.
    latest_prediction = max(prediction_rows, key=_prediction_precedence, default=None)

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
    match_stats = await _fetch_match_stats(db, sport_slug, league_slug, fixture, live_state_row)

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
                extra_markets=_build_extra_markets(
                    latest_prediction, (await bulk_corners_reference(db, [fixture])).get(fixture.id)
                ),
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
        match_stats=match_stats,
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
    odds_rows = _newest_odds_per_book(
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
    fixture = (
        await db.execute(select(Fixture).where(Fixture.id == fixture_id))
    ).scalar_one_or_none()
    reference = (await bulk_corners_reference(db, [fixture] if fixture else [])).get(fixture_id)
    return PredictionResponse(
        model_version=latest.model_version,
        home_prob=latest.home_prob,
        draw_prob=latest.draw_prob,
        away_prob=latest.away_prob,
        confidence_tier=latest.confidence_tier.value,
        expected_value=latest.expected_value,
        extra_markets=_build_extra_markets(latest, reference),
    )
