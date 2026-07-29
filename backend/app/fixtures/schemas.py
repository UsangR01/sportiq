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
    """The model's favoured outcome for this fixture, with the best available odds for it —
    the same selection/probability/odds math app/picks/service.py already computes for
    /picks, surfaced inline per fixture in /fixtures's list view instead of requiring a
    separate call per fixture. odds is null when a prediction exists but no odds have been
    ingested yet (real for Brasileirão fixtures before TheRundown/API-Football odds land)."""

    selection: str  # "home" | "draw" | "away"
    probability: float
    odds: float | None = None


class LiveStateResponse(BaseModel):
    home_score: int
    away_score: int
    match_minute: int | None = None
    period: str | None = None
    status: str
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
    best_pick: BestPick | None = None
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


class FixtureDetail(FixtureSummary):
    odds: list[OddsLineResponse] = []
    prediction: PredictionResponse | None = None
    home_team_form: TeamFeaturesResponse | None = None
    away_team_form: TeamFeaturesResponse | None = None
