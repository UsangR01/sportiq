import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    email: EmailStr
    password: str
    # strict=True on the model would otherwise reject this field outright: pydantic-core's
    # strict UUID validator requires an actual UUID instance, which no JSON body can ever
    # supply (JSON has no UUID type) — every real client sending a guest_session_id here hit
    # a 422 until this per-field override. Discovered live once a real client (the mobile
    # app) first exercised the guest-session-migration path with a non-null id.
    guest_session_id: uuid.UUID | None = Field(default=None, strict=False)


class LoginRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class GuestSessionState(BaseModel):
    model_config = ConfigDict(strict=True)

    sport_filter: str | None = None
    min_odds: float | None = None
    odds_format: str | None = None


class GuestSessionResponse(BaseModel):
    guest_session_id: uuid.UUID
