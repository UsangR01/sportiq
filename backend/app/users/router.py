from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import get_current_user
from app.core.database import get_db
from app.users.models import User, UserPreference
from app.users.schemas import UserPreferencesResponse, UserPreferencesUpdate

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

    await db.commit()
    await db.refresh(prefs)

    return UserPreferencesResponse(
        default_sport_id=prefs.default_sport_id,
        default_min_odds=prefs.default_min_odds,
        odds_format=prefs.odds_format.value,
    )
