import logging
import uuid
from types import SimpleNamespace
from typing import Any

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
            saved_market=item.saved_market,
            saved_selection=item.saved_selection,
            saved_line=item.saved_line,
            saved_probability=item.saved_probability,
            saved_odds=item.saved_odds,
        )
        for item, fixture, sport_slug, league_slug, home, away in rows
    ]


def _receipt(body: WatchlistAdd, candidates: list) -> Any | None:
    """The pick the CLIENT says it was showing, confirmed against the real candidate set.

    Reported as: saved "Bodo/Glimt HOME at 1.55" from the feed and the saved page then read
    "1X at 1.17". Both numbers were right about their own question. The card ranks only
    candidates that clear the user's odds slider (1.20 by default), so 1.17 was never eligible
    there; this endpoint recomputed with NO floor, where 1X wins on probability. The receipt
    recorded a pick that had never been on screen.

    The sliders are device state and are not persisted, so the server genuinely cannot re-derive
    what was displayed -- the client has to say. It is CONFIRMED rather than trusted: the
    market/selection/line must exist among this fixture's real candidates, so a stale or broken
    client cannot invent a market. The probability and price are then stored AS DISPLAYED, which
    is the whole point of a receipt -- if odds moved between render and tap, the user acted on
    what they saw, not on what the row says a second later.

    Returns None when the client sent nothing (older builds) or when the claim does not match,
    leaving the caller to fall back to recomputation.
    """
    if not body.shown_market or not body.shown_selection:
        return None
    for candidate in candidates:
        if candidate.market != body.shown_market or candidate.selection != body.shown_selection:
            continue
        # Lines must agree as a pair: a totals market without a line, or a line that differs,
        # is a different bet -- under 9.5 corners is not under 10.5.
        if (candidate.line is None) != (body.shown_line is None):
            continue
        if candidate.line is not None and abs(candidate.line - body.shown_line) > 1e-9:
            continue
        return SimpleNamespace(
            market=body.shown_market,
            selection=body.shown_selection,
            line=body.shown_line,
            # Fall back to the server's figure per field, so a client that sends the selection
            # but omits a price still records the right MARKET rather than nothing at all.
            probability=(
                body.shown_probability
                if body.shown_probability is not None
                else candidate.probability
            ),
            odds=body.shown_odds if body.shown_odds is not None else candidate.odds,
        )
    logger.warning(
        "Saved pick %s/%s/%s is not among fixture %s's candidates; recomputing instead",
        body.shown_market,
        body.shown_selection,
        body.shown_line,
        body.fixture_id,
    )
    return None


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

    # RECORD THE PICK AS IT IS SHOWN RIGHT NOW, because this is the only moment it can be
    # captured honestly. best_pick is recomputed on every request and never stored, so by the
    # time the user opens their watchlist the card may legitimately say something else -- odds
    # land, features refresh, the model is re-run. Freezing the whole feed would mean showing
    # everyone a number we already know is stale; freezing what THIS user acted on costs
    # nobody else anything.
    #
    # A failure here must not lose the save: the fixture is what the user asked to keep, and a
    # missing receipt is a smaller loss than a missing watchlist entry.
    saved = None
    try:
        from app.fixtures.router import _bulk_best_picks

        best, all_candidates = await _bulk_best_picks(db, [body.fixture_id])
        saved = _receipt(body, all_candidates.get(body.fixture_id) or []) or best.get(
            body.fixture_id
        )
    except Exception:
        logger.exception("Could not capture the shown pick for fixture %s", body.fixture_id)

    db.add(
        WatchlistItem(
            user_id=user.id,
            fixture_id=body.fixture_id,
            saved_market=saved.market if saved else None,
            saved_selection=saved.selection if saved else None,
            saved_line=saved.line if saved else None,
            saved_probability=saved.probability if saved else None,
            saved_odds=saved.odds if saved else None,
        )
    )
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
