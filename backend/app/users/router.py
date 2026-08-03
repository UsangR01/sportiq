from exponent_server_sdk import PushClient
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import get_current_user
from app.core.database import get_db
from app.users.models import User, UserPreference
from app.users.schemas import PushTokenUpdate, UserPreferencesResponse, UserPreferencesUpdate

router = APIRouter(tags=["users"])


@router.get("/user/preferences", response_model=UserPreferencesResponse)
async def get_preferences(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    prefs = (
        await db.execute(select(UserPreference).where(UserPreference.user_id == user.id))
    ).scalar_one_or_none()
    if prefs is None:
        return UserPreferencesResponse(odds_format="decimal")
    return UserPreferencesResponse(
        default_sport_id=prefs.default_sport_id,
        default_min_odds=prefs.default_min_odds,
        odds_format=prefs.odds_format.value,
        theme_preference=prefs.theme_preference.value,
    )


@router.put("/user/preferences", response_model=UserPreferencesResponse)
async def update_preferences(
    body: UserPreferencesUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    prefs = (
        await db.execute(select(UserPreference).where(UserPreference.user_id == user.id))
    ).scalar_one_or_none()

    if prefs is None:
        prefs = UserPreference(user_id=user.id, odds_format="decimal")
        db.add(prefs)

    if body.default_sport_id is not None:
        prefs.default_sport_id = body.default_sport_id
    if body.default_min_odds is not None:
        prefs.default_min_odds = body.default_min_odds
    if body.odds_format is not None:
        prefs.odds_format = body.odds_format
    if body.theme_preference is not None:
        prefs.theme_preference = body.theme_preference

    await db.commit()
    await db.refresh(prefs)

    return UserPreferencesResponse(
        default_sport_id=prefs.default_sport_id,
        default_min_odds=prefs.default_min_odds,
        odds_format=prefs.odds_format.value,
        theme_preference=prefs.theme_preference.value,
    )


@router.put("/user/push-token", status_code=status.HTTP_204_NO_CONTENT)
async def update_push_token(
    body: PushTokenUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Registers (or clears, if expo_push_token is null — the device disabled push
    notifications) this device's Expo push token (TDD §5.4) — read by
    app/workers/notify_users.py's recipient query. Auth-gated: push notifications are tied
    to a user's saved preferences (min_odds, sport), which only exist for logged-in users."""
    if body.expo_push_token is not None and not PushClient.is_exponent_push_token(
        body.expo_push_token
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Not a valid Expo push token",
        )
    user.expo_push_token = body.expo_push_token
    await db.commit()
