import uuid
from datetime import datetime
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


class WatchlistAdd(BaseModel):
    fixture_id: uuid.UUID


class WatchlistItemResponse(BaseModel):
    """The saved row plus enough fixture detail to render a list without a second call.

    Deliberately NOT the full FixtureSummary: that carries best_pick/all_market_picks, which
    are computed per request across every market and would make listing a watchlist as
    expensive as loading the feed. A client wanting live odds for a saved fixture already has
    GET /fixtures/{id}.
    """

    fixture_id: uuid.UUID
    sport_slug: str
    league_slug: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    kickoff_is_estimated: bool = False
    status: str
    created_at: datetime
