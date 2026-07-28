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


class PushTokenUpdate(BaseModel):
    # None clears the token (device disabled push notifications) — see PUT /user/push-token.
    expo_push_token: str | None
