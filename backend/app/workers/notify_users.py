import asyncio
import logging
import uuid

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
from app.fixtures.models import Fixture
from app.odds.models import Odds
from app.picks.service import best_available_odds, best_outcome
from app.predictions.models import ConfidenceTier, Prediction
from app.users.models import User, UserPreference
from app.workers.celery import celery_app

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
    asyncio.run(_notify_new_pick(uuid.UUID(fixture_id), uuid.UUID(prediction_id)))


@celery_app.task(name="app.workers.notify_users.notify_kickoff_reminder")
def notify_kickoff_reminder(fixture_id: str) -> None:
    """T-60 min kickoff reminder for fixtures in a user's watchlist, scheduled via Celery's
    eta parameter (TDD §5.4). Not implemented — no watchlist table exists in the TDD §2.1
    schema (PICK-07 "save to watchlist" is a Could-Have, Phase 2 requirement)."""
    raise NotImplementedError("No watchlist table exists yet — see PICK-07 (Phase 2)")
