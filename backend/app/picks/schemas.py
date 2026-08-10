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
    # UNVALIDATED — do not render this to users.
    #
    # Measured on settled pre-match predictions (2026-08-10): HIGH claimed 74.1% and delivered
    # 60.9% (n=69), while MEDIUM claimed 57.8% and delivered 68.5% (n=89). The label pointed at
    # the weaker set, so it was removed from the app and from the push gate. It is still
    # computed and returned as MEASUREMENT DATA, so the thresholds can be recalibrated against
    # outcomes once the sample supports it — not as advice.
    confidence_tier: str
