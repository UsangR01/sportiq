import uuid
from typing import Literal

from pydantic import BaseModel

# Kept in lock-step with app/users/models.py:ThemePreference. Literal rather than the enum
# itself so the wire format is the lowercase value ("dark") the mobile client already uses,
# independent of the uppercase member names Postgres stores.
ThemePreferenceValue = Literal["light", "dark", "system"]


class UserPreferencesResponse(BaseModel):
    default_sport_id: uuid.UUID | None = None
    default_min_odds: float | None = None
    odds_format: str
    theme_preference: ThemePreferenceValue = "system"


class UserPreferencesUpdate(BaseModel):
    default_sport_id: uuid.UUID | None = None
    default_min_odds: float | None = None
    odds_format: str | None = None
    # None means "not being changed in this request", matching every other field here — it is
    # not a way to clear the setting, since "system" is the explicit follow-the-OS value.
    theme_preference: ThemePreferenceValue | None = None


class PushTokenUpdate(BaseModel):
    # None clears the token (device disabled push notifications) — see PUT /user/push-token.
    expo_push_token: str | None
