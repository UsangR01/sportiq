import uuid

from pydantic import BaseModel, ConfigDict, EmailStr


class RegisterRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    email: EmailStr
    password: str
    guest_session_id: uuid.UUID | None = None


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
