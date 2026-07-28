import uuid

from pydantic import BaseModel


class UserPreferencesResponse(BaseModel):
    default_sport_id: uuid.UUID | None = None
    default_min_odds: float | None = None
    odds_format: str


class UserPreferencesUpdate(BaseModel):
    default_sport_id: uuid.UUID | None = None
    default_min_odds: float | None = None
    odds_format: str | None = None
