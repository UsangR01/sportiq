import uuid
from datetime import date, datetime

from pydantic import BaseModel


class HistoryEntry(BaseModel):
    fixture_id: uuid.UUID
    model_version: str
    predicted_probability: float
    confidence_tier: str
    result: str
    settled_at: datetime


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
