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
    accuracy: float
    rps_score: float
    roi_simulation: float
