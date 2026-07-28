import uuid
from datetime import datetime

from pydantic import BaseModel


class PickResponse(BaseModel):
    fixture_id: uuid.UUID
    sport_slug: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    selection: str  # "home" | "draw" | "away"
    odds: float
    model_probability: float
    expected_value: float
    confidence_tier: str
