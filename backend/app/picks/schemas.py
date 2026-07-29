import uuid
from datetime import datetime

from pydantic import BaseModel


class PickResponse(BaseModel):
    fixture_id: uuid.UUID
    sport_slug: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    market: str = "h2h"  # "h2h" | "double_chance" | "goals_total" | "corners_total"
    # "home" | "draw" | "away" (h2h); "1X" | "X2" (double_chance); "over" | "under" (totals)
    selection: str
    # Only set for goals_total/corners_total — the totals line this pick is for (e.g. 2.5, 9.5).
    line: float | None = None
    odds: float
    model_probability: float
    expected_value: float
    confidence_tier: str
