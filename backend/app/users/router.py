import logging
import uuid

from exponent_server_sdk import PushClient
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.auth.security import get_current_user
from app.core.database import get_db
from app.fixtures.models import Fixture, Team
from app.sports.models import League, Sport
from app.users.models import User, UserPreference, WatchlistItem
from app.users.schemas import (
    PushTokenUpdate,
    UserPreferencesResponse,
    UserPreferencesUpdate,
    WatchlistAdd,
    WatchlistItemResponse,
)

logger = logging.getLogger(__name__)

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


@router.get("/user/watchlist", response_model=list[WatchlistItemResponse])
async def list_watchlist(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Fixtures this user saved (PRD PICK-07).

    Ordered by kickoff so the next match to start is first — a watchlist is read to answer
    "what am I waiting on", not "what did I save most recently". Completed fixtures are kept
    rather than hidden: a saved match is still worth seeing the result of, and the client
    already renders a score for a completed fixture.
    """
    # Two aliases of Team — one join each for home and away — since a single Team entity cannot
    # be joined twice in one statement.
    home_team = aliased(Team)
    away_team = aliased(Team)
    rows = (
        await db.execute(
            select(WatchlistItem, Fixture, Sport.slug, League.slug, home_team, away_team)
            .join(Fixture, Fixture.id == WatchlistItem.fixture_id)
            .join(Sport, Sport.id == Fixture.sport_id)
            .join(League, League.id == Fixture.league_id)
            .join(home_team, home_team.id == Fixture.home_team_id)
            .join(away_team, away_team.id == Fixture.away_team_id)
            .where(WatchlistItem.user_id == user.id)
            .order_by(Fixture.kickoff_utc)
        )
    ).all()
    return [
        WatchlistItemResponse(
            fixture_id=fixture.id,
            sport_slug=sport_slug,
            league_slug=league_slug,
            home_team=home.name,
            away_team=away.name,
            kickoff_utc=fixture.kickoff_utc,
            kickoff_is_estimated=fixture.kickoff_is_estimated,
            status=fixture.status.value.lower(),
            created_at=item.created_at,
        )
        for item, fixture, sport_slug, league_slug, home, away in rows
    ]


@router.post("/user/watchlist", status_code=status.HTTP_204_NO_CONTENT)
async def add_to_watchlist(
    body: WatchlistAdd,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Saves a fixture. Idempotent: saving an already-saved fixture succeeds and changes
    nothing, so a double tap on a flaky connection cannot create two reminders (the
    uq_watchlist_user_fixture constraint backs this up at the DB level)."""
    fixture = (
        await db.execute(select(Fixture).where(Fixture.id == body.fixture_id))
    ).scalar_one_or_none()
    if fixture is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fixture not found")

    existing = (
        await db.execute(
            select(WatchlistItem).where(
                WatchlistItem.user_id == user.id,
                WatchlistItem.fixture_id == body.fixture_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    db.add(WatchlistItem(user_id=user.id, fixture_id=body.fixture_id))
    await db.commit()

    # Arm the T-60 reminder now, not at the next daily sweep. ingest_fixtures runs at 02:00
    # UTC, so without this a fixture saved during the day for a match later the same day or
    # the next morning is never queued and the reminder silently never arrives — the moment a
    # user saves is exactly when they expect it to be set.
    #
    # Imported here rather than at module scope: app.workers imports the API models, so a
    # top-level import would be circular. A broker that is down must not fail the save either
    # — the fixture is stored, and the daily sweep still picks it up.
    from app.workers.ingest_fixtures import schedule_kickoff_reminder

    try:
        schedule_kickoff_reminder(fixture)
    except Exception:
        logger.exception("Could not queue kickoff reminder for fixture %s", body.fixture_id)


@router.delete("/user/watchlist/{fixture_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_from_watchlist(
    fixture_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Removes a saved fixture. Also idempotent — removing something not saved is a 204, not a
    404, so the client never has to reconcile which state it thought it was in."""
    await db.execute(
        delete(WatchlistItem).where(
            WatchlistItem.user_id == user.id, WatchlistItem.fixture_id == fixture_id
        )
    )
    await db.commit()
