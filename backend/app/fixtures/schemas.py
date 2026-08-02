import uuid
from datetime import datetime

from pydantic import BaseModel


class TeamFeaturesResponse(BaseModel):
    elo_rating: float | None = None
    attack_str: float | None = None
    defence_str: float | None = None
    form_pts_5: float | None = None
    xg_for_5: float | None = None
    xg_against_5: float | None = None
    days_since_last_match: int | None = None
    home_win_rate: float | None = None
    away_win_rate: float | None = None


class BestPick(BaseModel):
    """The model's single favoured outcome for this fixture, with the best available odds for
    it — drawn from ACROSS every market this product supports (h2h, double chance, Over/Under
    goals, Over/Under corners), not just home/draw/away, per the user's explicit ask that the
    Home/Picks feed surface "the best odds with the highest probability of winning" regardless
    of which market that happens to live in. See app/fixtures/router.py:_pick_best. odds is
    null when a prediction exists but no odds have been ingested yet for ANY market (real for
    Brasileirão fixtures before TheRundown/API-Football odds land, or for a market a league's
    odds provider simply doesn't cover — see CLAUDE.md's per-league odds-coverage notes)."""

    # "home"|"draw"|"away" (h2h); "1X"|"X2" (double_chance); "over"|"under" (totals)
    selection: str
    probability: float
    odds: float | None = None
    market: str = "h2h"  # "h2h" | "double_chance" | "goals_total" | "corners_total"
    line: float | None = None  # goals_total/corners_total only


class LiveStateResponse(BaseModel):
    home_score: int
    away_score: int
    match_minute: int | None = None
    period: str | None = None
    status: str
    # Football only — real corner-kick counts, fetched once at settlement time (see
    # app/workers/ingest_fixtures.py:_maybe_settle_outcome). Null for NBA and for any fixture
    # settled before this existed — used to give the Over/Under corners market a real
    # win/loss verdict instead of staying permanently unverifiable.
    home_corners: int | None = None
    away_corners: int | None = None
    # Null for a normally-played-out result; "retired"/"walkover" for one that ended without
    # being played out (tennis in practice). The mobile feed shows a neutral "RET" badge and
    # withholds the win/loss verdict for these rather than counting them against the model,
    # since bookmakers generally void bets on a retirement.
    result_type: str | None = None
    last_updated_utc: datetime


class FixtureSummary(BaseModel):
    id: uuid.UUID
    sport_slug: str
    league_slug: str
    league_name: str
    league_country: str | None = None
    home_team: str
    away_team: str
    kickoff_utc: datetime
    status: str
    season: str
    # Tennis only — a tour (ATP/WTA) is one league, so the feed groups by TOURNAMENT instead,
    # giving users something they can actually find in a betting app. Null for football/NBA,
    # where league_name/league_country already serve that role. `tournament_location` is a
    # CITY, not a country (the provider exposes no country field) — the client maps it.
    tournament_name: str | None = None
    tournament_surface: str | None = None
    tournament_location: str | None = None
    best_pick: BestPick | None = None
    # Every real candidate across all four markets (h2h, double chance, goals/corners O/U) —
    # NOT just best_pick's single winner — so a past/completed fixture can show a full
    # win/loss breakdown across every market for evaluating model performance, per explicit
    # user request ("I need all markets predicted in the past to still be shown... Over and
    # Under, double chances, corners. Everything should be shown"). Always the FULL,
    # market-param-independent list (unlike best_pick, which respects GET /fixtures's own
    # market/line restriction) — see app/fixtures/router.py:_bulk_best_picks.
    all_market_picks: list[BestPick] = []
    # Present for both in-progress and completed fixtures (see
    # app/workers/ingest_fixtures.py:_upsert_live_state) — null for a fixture that hasn't
    # started. Surfaced in the list view too, not just fixture detail, so the Home feed can
    # show a score inline without a per-fixture call.
    live_state: LiveStateResponse | None = None


class OddsLineResponse(BaseModel):
    bookmaker: str
    market: str
    home_odds: float | None = None
    draw_odds: float | None = None
    away_odds: float | None = None
    updated_at: datetime


class TotalsProbability(BaseModel):
    """One Over/Under line's calibrated probability pair — see app/models_ml/markets.py.
    under_prob/over_prob are both null when the underlying expected total (xg_home+xg_away
    for goals, corners_xg_home+corners_xg_away for corners) isn't available yet (e.g. an
    artefact trained before the corners regressors existed)."""

    line: float
    under_prob: float | None = None
    over_prob: float | None = None


class ExtraMarketsResponse(BaseModel):
    """Football-only prediction markets beyond the core home/draw/away 1X2 — double chance and
    Over/Under goals/corners (see app/models_ml/markets.py). None/empty fields mean the
    underlying inputs aren't available (e.g. NBA has no draw_prob, so double chance is null;
    an older prediction has no corners_xg_*, so corners_totals is empty), never a fabricated
    50/50 split."""

    double_chance_home_or_draw_prob: float | None = None
    double_chance_away_or_draw_prob: float | None = None
    goals_totals: list[TotalsProbability] = []
    corners_totals: list[TotalsProbability] = []


class PredictionResponse(BaseModel):
    model_version: str
    home_prob: float
    draw_prob: float | None = None
    away_prob: float
    confidence_tier: str
    expected_value: float | None = None
    extra_markets: ExtraMarketsResponse | None = None


class HeadToHeadResponse(BaseModel):
    """Real head-to-head history between this fixture's two teams — replaces the raw
    bookmaker-odds table on the fixture detail screen per direct user request ("Users don't
    find the Odds section useful... replaced with H2H statistics"). Per a follow-up ask,
    shows average goals/corners/shots/shots-on-goal/possession over the last 5 real meetings
    per side instead of a list of individual match scores ("important stats that will give
    users confidence on the prediction"). home_wins/draws/away_wins and every avg_*_home/away
    field are relative to THIS fixture's home/away assignment, not each past meeting's own —
    see app/adapters/api_football.py:H2HDetail. Football only for now (no equivalent built for
    NBA yet — see app/fixtures/router.py:get_fixture); null, not a fabricated empty record,
    when unavailable. Each avg_* field is independently null when none of the counted meetings
    had a real value for that specific stat (never a fabricated average)."""

    meetings_count: int
    home_wins: int
    draws: int
    away_wins: int
    avg_goals_home: float | None = None
    avg_goals_away: float | None = None
    avg_corners_home: float | None = None
    avg_corners_away: float | None = None
    avg_shots_home: float | None = None
    avg_shots_away: float | None = None
    avg_shots_on_goal_home: float | None = None
    avg_shots_on_goal_away: float | None = None
    avg_possession_home: float | None = None
    avg_possession_away: float | None = None


class FixtureDetail(FixtureSummary):
    odds: list[OddsLineResponse] = []
    prediction: PredictionResponse | None = None
    home_team_form: TeamFeaturesResponse | None = None
    away_team_form: TeamFeaturesResponse | None = None
    head_to_head: HeadToHeadResponse | None = None
