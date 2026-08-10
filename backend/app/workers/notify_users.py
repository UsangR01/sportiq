import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import requests
from exponent_server_sdk import (
    DeviceNotRegisteredError,
    PushClient,
    PushMessage,
    PushServerError,
    PushTicket,
    PushTicketError,
)
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.predictions.models import Prediction
from app.users.models import PushTicketRecord, User, UserPreference, WatchlistItem
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
        return
    except (PushServerError, PushTicketError) as exc:
        logger.warning("Expo push send failed for user %s: %s", user.id, exc)
        return

    # A ticket means ACCEPTED, not DELIVERED. The real outcome — including a wrong FCM
    # credential, which fails every send while every ticket still looks fine — is only visible
    # in the receipt, fetched later by check_push_receipts. The id was previously discarded,
    # which made that entire class of failure invisible.
    if getattr(ticket, "id", None):
        db.add(PushTicketRecord(ticket_id=ticket.id, user_id=user.id))
        await db.commit()


def format_selection(pick) -> str:
    """Human-readable selection, including the market when it is not a plain 1X2 call.

    "under @ 1.30" is meaningless without knowing under WHAT — the previous body only ever
    described h2h picks, so it never had to say."""
    if pick.market in ("goals_total", "corners_total") and pick.line is not None:
        what = "goals" if pick.market == "goals_total" else "corners"
        return f"{pick.selection.upper()} {pick.line} {what}"
    return pick.selection.upper()


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

        # Gated on the FEED'S OWN pick selection, not on the confidence tier.
        #
        # The tier gate was removed because it selected the tier that measured WORST: on settled
        # pre-match predictions, HIGH claimed 74.1% and delivered 60.9% (n=69) while MEDIUM
        # claimed 57.8% and delivered 68.5% (n=89). The most intrusive channel was pointing at
        # the weaker set, and the badge has already been hidden from users for the same reason.
        #
        # This is NOT a new rule invented on that evidence -- n=69 is far too thin to justify
        # one, and inventing a replacement was explicitly refused. It is the rule the product
        # ALREADY applies to decide a pick is worth showing: _bulk_best_picks runs the same
        # base-rate edge, market-disagreement and feature-completeness guards the feed uses.
        # Notifying about a pick the feed itself would not surface was never defensible.
        #
        # It also fixes a real incoherence. The previous path used best_outcome, which only
        # considers h2h, while the card shows the best pick ACROSS markets -- so a push could
        # say "back home" while the app showed "UNDER 3.5" for the same fixture. Now they are
        # the same selection by construction.
        #
        # Volume control comes from the guards plus each user's own saved min_odds, which is a
        # preference they set, rather than from a threshold we never validated.
        from app.fixtures.router import _bulk_best_picks

        best_picks, _all_picks = await _bulk_best_picks(db, [fixture_id])
        pick = best_picks.get(fixture_id)
        if pick is None or pick.odds is None:
            # No pick the feed would surface, or no real price to quote. Either way there is
            # nothing worth interrupting someone for.
            return

        recipients = await _recipients_for_pick(db, fixture, min_odds_met=pick.odds)

        for user in recipients:
            await _send_push(
                db,
                user,
                # No longer "high-confidence": that label came from a tier measured as
                # misleading and hidden from the app for the same reason. The notification
                # should not claim something the product itself has stopped asserting.
                title="New pick",
                body=(
                    f"{format_selection(pick)} @ {pick.odds} — "
                    f"{pick.probability:.0%} model probability"
                ),
                data={"fixture_id": str(fixture_id)},
            )


@celery_app.task(name="app.workers.notify_users.notify_new_pick")
def notify_new_pick(fixture_id: str, prediction_id: str) -> None:
    """Queued by run_predictions for every new prediction (TDD §5.4); _notify_new_pick decides
    whether it is worth sending, using the feed's own pick selection.

    It was previously described as firing on HIGH-confidence predictions. run_predictions never
    actually gated on the tier — the filter lived downstream — and that tier has since been
    measured as misleading and removed from the gate entirely. Deep link on tap:
    sportpiq://fixture/{fixture_id}, handled by Expo Router."""
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
                data={"fixture_id": str(fixture_id), "url": f"sportpiq://fixture/{fixture_id}"},
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


# Expo does not populate a receipt the instant a ticket is issued. Waiting before asking avoids
# a round of "not ready yet" lookups that would have to be retried anyway.
PUSH_RECEIPT_MIN_AGE_MINUTES = 15
# Expo caps a single getReceipts call; the SDK exposes its own limit, used rather than guessed.
PUSH_RECEIPT_BATCH = PushClient.DEFAULT_MAX_RECEIPT_COUNT


async def _check_push_receipts() -> None:
    """Read the delivery receipts for tickets issued at least PUSH_RECEIPT_MIN_AGE_MINUTES ago.

    A ticket says Expo ACCEPTED the message. Only the receipt says what happened to it, and
    that is where the failures that matter live: DeviceNotRegistered (stale token),
    MessageTooBig, MessageRateExceeded, and InvalidCredentials — a wrong FCM credential, which
    fails every send for every user while every ticket still reports success. Without this the
    whole notification feature could be silently dead and the logs would look healthy.

    Checked rows are DELETED rather than flagged: this is a work queue, not history. Keeping
    them would grow a table forever to record that a notification worked.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=PUSH_RECEIPT_MIN_AGE_MINUTES)
    async with async_session_factory() as db:
        pending = (
            (
                await db.execute(
                    select(PushTicketRecord)
                    .where(PushTicketRecord.created_at <= cutoff)
                    .order_by(PushTicketRecord.created_at)
                    .limit(PUSH_RECEIPT_BATCH)
                )
            )
            .scalars()
            .all()
        )
        if not pending:
            return

        by_ticket = {row.ticket_id: row for row in pending}
        try:
            receipts = await asyncio.to_thread(
                _push_client().check_receipts,
                [SimpleNamespace(id=ticket_id) for ticket_id in by_ticket],
            )
        except (PushServerError, requests.ConnectionError, requests.HTTPError) as exc:
            # Leave the rows in place so the next run retries. A lookup failure is not a
            # delivery failure, and deleting here would lose the only record that could
            # surface a real one.
            logger.warning("Could not fetch Expo push receipts: %s", exc)
            return

        for receipt in receipts:
            record = by_ticket.get(getattr(receipt, "id", None))
            if receipt.is_success():
                continue
            details = getattr(receipt, "details", None) or {}
            error = details.get("error")
            if error == PushTicket.ERROR_DEVICE_NOT_REGISTERED and record is not None:
                user = (
                    await db.execute(select(User).where(User.id == record.user_id))
                ).scalar_one_or_none()
                if user is not None:
                    user.expo_push_token = None
                logger.info("Cleared a push token Expo reported as unregistered")
            elif error == "InvalidCredentials":
                # Not per-device: every send is failing. Loud on purpose — this is the failure
                # the whole receipt check exists to make visible.
                logger.error(
                    "Expo rejected the push credentials (%s). Every notification is failing; "
                    "check the FCM/APNs credentials on the EAS project.",
                    receipt.message,
                )
            else:
                logger.warning("Expo push receipt error %s: %s", error, receipt.message)

        for row in pending:
            await db.delete(row)
        await db.commit()
        logger.info("Checked %d Expo push receipt(s)", len(pending))


@celery_app.task(name="app.workers.notify_users.check_push_receipts")
def check_push_receipts() -> None:
    run_task(_check_push_receipts())
