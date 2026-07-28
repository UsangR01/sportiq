import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.guest_session import create_guest_session, get_guest_session, set_guest_session
from app.auth.models import RefreshToken
from app.auth.schemas import (
    GuestSessionResponse,
    GuestSessionState,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
)
from app.auth.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.core.database import get_db
from app.core.limiter import limiter
from app.core.redis import get_redis
from app.sports.models import Sport
from app.users.models import User, UserPreference

router = APIRouter(tags=["auth"])


async def _issue_token_pair(db: AsyncSession, user: User) -> TokenPair:
    access_token = create_access_token(str(user.id))
    raw_refresh, refresh_hash, expires_at = generate_refresh_token()
    db.add(RefreshToken(user_id=user.id, token_hash=refresh_hash, expires_at=expires_at))
    await db.commit()
    return TokenPair(access_token=access_token, refresh_token=raw_refresh)


@router.post("/auth/register", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(email=body.email, password_hash=hash_password(body.password))
    db.add(user)
    await db.flush()

    if body.guest_session_id is not None:
        redis = get_redis()
        guest_state = await get_guest_session(redis, body.guest_session_id)
        if guest_state:
            default_sport_id = None
            if guest_state.get("sport_filter"):
                sport = await db.execute(
                    select(Sport).where(Sport.slug == guest_state["sport_filter"])
                )
                sport_row = sport.scalar_one_or_none()
                default_sport_id = sport_row.id if sport_row else None

            db.add(
                UserPreference(
                    user_id=user.id,
                    default_sport_id=default_sport_id,
                    default_min_odds=guest_state.get("min_odds"),
                    odds_format=guest_state.get("odds_format") or "decimal",
                )
            )

    return await _issue_token_pair(db, user)


@router.post("/auth/login", response_model=TokenPair)
@limiter.limit("5/minute")
async def login(request: Request, body: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    return await _issue_token_pair(db, user)


@router.post("/auth/refresh", response_model=TokenPair)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    token_hash = hash_refresh_token(body.refresh_token)
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    stored = result.scalar_one_or_none()

    now = datetime.now(UTC)
    if stored is None or stored.revoked_at is not None or stored.expires_at < now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    # Rotate: invalidate the presented token before issuing a new pair.
    stored.revoked_at = now

    user_result = await db.execute(select(User).where(User.id == stored.user_id))
    user = user_result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    return await _issue_token_pair(db, user)


@router.post("/guest/session", response_model=GuestSessionResponse)
async def create_session(response: Response):
    redis = get_redis()
    guest_session_id = await create_guest_session(redis)
    response.set_cookie(
        key="guest_session_id",
        value=str(guest_session_id),
        httponly=True,
        samesite="strict",
        max_age=24 * 60 * 60,
    )
    return GuestSessionResponse(guest_session_id=guest_session_id)


@router.put("/guest/session/{guest_session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def update_session(guest_session_id: uuid.UUID, body: GuestSessionState):
    redis = get_redis()
    await set_guest_session(redis, guest_session_id, body.model_dump(exclude_none=True))
