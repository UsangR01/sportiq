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


class LiveStateResponse(BaseModel):
    home_score: int
    away_score: int
    match_minute: int | None = None
    period: str | None = None
    status: str
    last_updated_utc: datetime


class OddsLineResponse(BaseModel):
    bookmaker: str
    market: str
    home_odds: float | None = None
    draw_odds: float | None = None
    away_odds: float | None = None
    updated_at: datetime


class PredictionResponse(BaseModel):
    model_version: str
    home_prob: float
    draw_prob: float | None = None
    away_prob: float
    confidence_tier: str
    expected_value: float | None = None


class FixtureDetail(FixtureSummary):
    live_state: LiveStateResponse | None = None
    odds: list[OddsLineResponse] = []
    prediction: PredictionResponse | None = None
    home_team_form: TeamFeaturesResponse | None = None
    away_team_form: TeamFeaturesResponse | None = None
