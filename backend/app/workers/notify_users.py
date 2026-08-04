import asyncio
import logging
import uuid
from datetime import UTC, datetime

import requests
from exponent_server_sdk import (
    DeviceNotRegisteredError,
    PushClient,
    PushMessage,
    PushServerError,
    PushTicketError,
)
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.odds.models import Odds
from app.picks.service import best_available_odds, best_outcome
from app.predictions.models import ConfidenceTier, Prediction
from app.users.models import User, UserPreference, WatchlistItem
from app.workers.celery import celery_app, run_task

logger = logging.getLogger(__name__)


async def _recipients_for_pick(db, fixture: Fixture, min_odds_met: float) -> list[User]:
    """All users whose saved min_odds preference is met for this pick's sport (TDD §5.4)."""
    rows = (
        (
            await db.execute(
                select(User)
                .join(UserPreference, UserPreference.user_id == User.id)
                .where(
                    UserPreference.default_sport_id == fixture.sport_id,
                    UserPreference.default_min_odds.is_not(None),
                    UserPreference.default_min_odds <= min_odds_met,
                    User.expo_push_token.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return rows


def _push_client() -> PushClient:
    settings = get_settings()
    session = requests.Session()
    session.headers.update(
        {
            "accept": "application/json",
            "accept-encoding": "gzip, deflate",
            "content-type": "application/json",
        }
    )
    if settings.expo_access_token:
        session.headers.update({"authorization": f"Bearer {settings.expo_access_token}"})
    return PushClient(session=session)


async def _send_push(db, user: User, title: str, body: str, data: dict) -> None:
    """POSTs to https://exp.host/--/api/v2/push/send via exponent-server-sdk (TDD §5.4),
    off the event loop since the SDK is synchronous (requests, not httpx). No
    EXPO_ACCESS_TOKEN has been provisioned for this project (see CLAUDE.md) — Expo's push
    API still accepts unauthenticated sends, just at a lower rate limit, so this can still
    work without one. A stale/uninstalled-app token self-heals: DeviceNotRegisteredError
    clears it so future notify runs stop retrying a dead token."""
    message = PushMessage(to=user.expo_push_token, title=title, body=body, data=data)
    try:
        ticket = await asyncio.to_thread(_push_client().publish, message)
        ticket.validate_response()
    except DeviceNotRegisteredError:
        user.expo_push_token = None
        await db.commit()
    except (PushServerError, PushTicketError) as exc:
        logger.warning("Expo push send failed for user %s: %s", user.id, exc)


async def _notify_new_pick(fixture_id: uuid.UUID, prediction_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        fixture = (
            await db.execute(select(Fixture).where(Fixture.id == fixture_id))
        ).scalar_one_or_none()
        prediction = (
            await db.execute(select(Prediction).where(Prediction.id == prediction_id))
        ).scalar_one_or_none()
        if fixture is None or prediction is None:
            return

        if prediction.confidence_tier != ConfidenceTier.HIGH:
            return

        odds_rows = [
            {"home_odds": o.home_odds, "draw_odds": o.draw_odds, "away_odds": o.away_odds}
            for o in (await db.execute(select(Odds).where(Odds.fixture_id == fixture_id)))
            .scalars()
            .all()
        ]
        best_odds = best_available_odds(odds_rows)
        outcome = best_outcome(
            prediction.home_prob,
            prediction.draw_prob,
            prediction.away_prob,
            best_odds["home"],
            best_odds["draw"],
            best_odds["away"],
        )
        if outcome is None:
            return

        recipients = await _recipients_for_pick(db, fixture, min_odds_met=outcome.odds)

        for user in recipients:
            await _send_push(
                db,
                user,
                title="New high-confidence pick",
                body=(
                    f"{outcome.selection} @ {outcome.odds} — "
                    f"{outcome.probability:.0%} model probability"
                ),
                data={"fixture_id": str(fixture_id)},
            )


@celery_app.task(name="app.workers.notify_users.notify_new_pick")
def notify_new_pick(fixture_id: str, prediction_id: str) -> None:
    """Queued by run_predictions when a new HIGH-confidence prediction is generated (TDD
    §5.4). Deep link on tap: sportiq://fixture/{fixture_id}, handled by Expo Router."""
    run_task(_notify_new_pick(uuid.UUID(fixture_id), uuid.UUID(prediction_id)))


async def _notify_kickoff_reminder(fixture_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        fixture = (
            await db.execute(select(Fixture).where(Fixture.id == fixture_id))
        ).scalar_one_or_none()
        if fixture is None:
            return
        # A postponed or already-finished fixture has nothing to remind anyone about. Worth
        # checking at send time rather than trusting the schedule: this task is queued with an
        # eta up to an hour out, and postponements are real — four Brasileirão fixtures were
        # postponed on one matchday during testing.
        if fixture.status is not FixtureStatus.SCHEDULED:
            logger.info(
                "Skipping kickoff reminder for %s — status is %s", fixture_id, fixture.status
            )
            return
        # An estimated kickoff is a DATE, not a time (see balldontlie_tennis.py) — "starts in
        # an hour" off a midnight placeholder would simply be wrong, so those are skipped
        # rather than sent with a fabricated precision.
        if fixture.kickoff_is_estimated:
            logger.info(
                "Skipping kickoff reminder for %s — kickoff is only an estimate", fixture_id
            )
            return

        home = (
            await db.execute(select(Team).where(Team.id == fixture.home_team_id))
        ).scalar_one_or_none()
        away = (
            await db.execute(select(Team).where(Team.id == fixture.away_team_id))
        ).scalar_one_or_none()
        matchup = f"{home.name if home else '?'} vs {away.name if away else '?'}"

        rows = (
            await db.execute(
                select(WatchlistItem, User)
                .join(User, User.id == WatchlistItem.user_id)
                .where(
                    WatchlistItem.fixture_id == fixture_id,
                    # reminded_at is the idempotency guard: Celery retries and an accidental
                    # re-queue must not notify the same person twice about one fixture.
                    WatchlistItem.reminded_at.is_(None),
                    User.expo_push_token.isnot(None),
                )
            )
        ).all()

        for item, user in rows:
            await _send_push(
                db,
                user,
                title="Starting soon",
                body=f"{matchup} kicks off in about an hour.",
                data={"fixture_id": str(fixture_id), "url": f"sportiq://fixture/{fixture_id}"},
            )
            item.reminded_at = datetime.now(UTC)
        await db.commit()
        logger.info("Kickoff reminder for %s sent to %d watcher(s)", fixture_id, len(rows))


@celery_app.task(name="app.workers.notify_users.notify_kickoff_reminder")
def notify_kickoff_reminder(fixture_id: str) -> None:
    """T-60 min kickoff reminder for everyone who saved this fixture (TDD §5.4, PRD PICK-07).

    Real now that watchlist_items exists — it was a NotImplementedError only because there was
    nowhere to read the recipients from. Queued with Celery's eta by
    ingest_fixtures.py:_queue_kickoff_reminders.
    """
    run_task(_notify_kickoff_reminder(uuid.UUID(fixture_id)))
