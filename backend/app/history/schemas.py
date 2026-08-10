import uuid
from datetime import date, datetime

from pydantic import BaseModel


class HistoryEntry(BaseModel):
    """One settled fixture the model made a real call on, with how that call turned out.

    Scored on the 1X2 market only (the model's argmax of home/draw/away) rather than on
    best_pick's cross-market choice. That is deliberate: best_pick depends on which odds
    happened to be ingested at the time, so scoring it would make historical accuracy a
    function of odds coverage rather than of the model. 1X2 is always present for every
    prediction, which makes this comparable across sports and across time."""

    fixture_id: uuid.UUID
    sport_slug: str
    league_slug: str
    home_team: str
    away_team: str
    model_version: str
    # The model's own confidence in the outcome it picked (not necessarily the home side).
    predicted_probability: float
    confidence_tier: str
    predicted_outcome: str  # "home" | "draw" | "away"
    result: str  # the real settled MatchResult
    was_correct: bool
    settled_at: datetime


class HistorySummary(BaseModel):
    """Aggregate model performance over settled fixtures — the real "how good is this thing"
    number, computed from actual results rather than from training-time metrics.

    Distinct from GET /stats/model, which reports what a model scored on its own held-out TEST
    SET at training time. This is the live equivalent: how the currently-serving predictions
    have actually fared on real fixtures since. The two can legitimately diverge, and that
    divergence is exactly what makes this worth surfacing."""

    sport_slug: str
    settled_fixtures: int
    correct: int
    accuracy: float
    # Fixtures excluded from the numbers above because there was nothing to score: a tennis
    # retirement or walkover, where bookmakers generally void bets. Counted and reported rather
    # than silently dropped, so the denominator is never quietly wrong.
    voided: int

    # --- honesty fields (docs/history-metrics-spec.md §6) -----------------------------------
    # An accuracy without these reads as a track record when it is often noise. Measured
    # 2026-08-10: at n=139 football, only a 10.5pp effect is detectable at 80% power, while the
    # edge we believe we have is 4.1pp. Reporting 56% with no sample context invites exactly
    # the conclusion the number cannot support.
    kind: str
    # 95% Wilson interval on `accuracy`. Wilson rather than normal-approximation because it
    # behaves at small n and near 0/1, which is precisely where this endpoint currently lives.
    accuracy_ci_low: float
    accuracy_ci_high: float
    # Smallest true effect this sample could detect at 80% power. When it exceeds the effect
    # being claimed, the number cannot settle the question either way.
    detectable_effect: float
    # False when settled_fixtures < MIN_REPORTABLE_N. Consumers should render "not enough data"
    # rather than a percentage.
    sufficient_sample: bool
    # Predictions excluded because their provenance could not be established — see
    # PredictionKind.UNKNOWN. Reported rather than hidden so the denominator stays auditable.
    excluded_unknown_provenance: int


class HistoryQuery(BaseModel):
    sport_slug: str | None = None
    league_slug: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    result: str | None = None


class ModelStats(BaseModel):
    sport_slug: str
    model_version: str
    accuracy: float | None = None
    rps_score: float | None = None
    # Small-sample, directional flat-stake backtest metric (see ml/training/train_nba.py) —
    # null for any model version trained before this column existed, or where no bookmaker
    # odds existed for the test-set games.
    roi_simulation: float | None = None
    trained_at: datetime
