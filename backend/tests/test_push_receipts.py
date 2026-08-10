"""Expo delivery receipts (notify_users.check_push_receipts).

Expo push is two-stage and only the first stage was ever observed here. publish() returns a
TICKET, meaning "accepted for delivery" — it is not a delivery. The outcome arrives later in a
RECEIPT, and that is the only place the failures that matter appear: a stale token, an
oversized message, and above all InvalidCredentials, which fails EVERY send for EVERY user
while every ticket still reports success. Before this, _send_push discarded the ticket id, so
the entire notification feature could have been dead with healthy-looking logs.

Only the third-party boundary is mocked, per this project's convention; the queue table is
real.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select

from app.auth.security import hash_password
from app.core.database import async_session_factory
from app.users.models import PushTicketRecord, User
from app.workers import notify_users


def _receipt(receipt_id, *, ok=True, error=None, message=""):
    return SimpleNamespace(
        id=receipt_id,
        is_success=lambda: ok,
        details={"error": error} if error else {},
        message=message,
    )


@pytest.fixture
async def user_with_ticket():
    async with async_session_factory() as db:
        user = User(
            email=f"receipt-{uuid.uuid4().hex[:8]}@test.local",
            password_hash=hash_password("x"),
            expo_push_token="ExponentPushToken[receipt-test]",
        )
        db.add(user)
        await db.flush()
        ticket = PushTicketRecord(
            ticket_id=f"tk-{uuid.uuid4().hex[:10]}",
            user_id=user.id,
            created_at=datetime.now(UTC) - timedelta(hours=1),  # old enough to have a receipt
        )
        db.add(ticket)
        await db.commit()
        ids = (user.id, ticket.ticket_id)
    yield ids
    async with async_session_factory() as db:
        await db.execute(delete(PushTicketRecord).where(PushTicketRecord.user_id == ids[0]))
        await db.execute(delete(User).where(User.id == ids[0]))
        await db.commit()


@pytest.mark.asyncio
async def test_a_device_not_registered_receipt_clears_the_token(user_with_ticket):
    """The self-healing path. The ticket said accepted; only the receipt reveals the device is
    gone, so without this the token is retried forever."""
    user_id, ticket_id = user_with_ticket
    receipts = [_receipt(ticket_id, ok=False, error="DeviceNotRegistered")]

    with patch.object(notify_users, "_push_client") as client:
        client.return_value.check_receipts.return_value = receipts
        await notify_users._check_push_receipts()

    async with async_session_factory() as db:
        token = (await db.execute(select(User.expo_push_token).where(User.id == user_id))).scalar()
    assert token is None


@pytest.mark.asyncio
async def test_invalid_credentials_is_logged_loudly(user_with_ticket, caplog):
    """THE failure this whole mechanism exists to surface: not one bad device, but every send
    failing because the FCM/APNs credential is wrong. It must not be a warning lost in noise."""
    import logging

    _user_id, ticket_id = user_with_ticket
    receipts = [
        _receipt(ticket_id, ok=False, error="InvalidCredentials", message="bad credentials")
    ]

    with caplog.at_level(logging.ERROR, logger=notify_users.__name__):
        with patch.object(notify_users, "_push_client") as client:
            client.return_value.check_receipts.return_value = receipts
            await notify_users._check_push_receipts()

    assert any("credentials" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_checked_ticket_is_removed_from_the_queue(user_with_ticket):
    """This is a work queue, not history. Keeping rows would grow a table forever to record
    that notifications worked."""
    user_id, ticket_id = user_with_ticket

    with patch.object(notify_users, "_push_client") as client:
        client.return_value.check_receipts.return_value = [_receipt(ticket_id)]
        await notify_users._check_push_receipts()

    async with async_session_factory() as db:
        remaining = (
            (await db.execute(select(PushTicketRecord).where(PushTicketRecord.user_id == user_id)))
            .scalars()
            .all()
        )
    assert remaining == []


@pytest.mark.asyncio
async def test_a_lookup_failure_leaves_the_ticket_for_the_next_run(user_with_ticket):
    """A failed lookup is not a failed delivery. Deleting on error would discard the only
    record that could later surface a real problem."""
    from exponent_server_sdk import PushServerError

    user_id, _ticket_id = user_with_ticket

    with patch.object(notify_users, "_push_client") as client:
        client.return_value.check_receipts.side_effect = PushServerError("boom", None)
        await notify_users._check_push_receipts()

    async with async_session_factory() as db:
        remaining = (
            (await db.execute(select(PushTicketRecord).where(PushTicketRecord.user_id == user_id)))
            .scalars()
            .all()
        )
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_a_ticket_too_new_is_not_checked_yet():
    """Expo does not populate a receipt immediately, so asking straight away just wastes a call
    on a result that has to be re-fetched anyway."""
    async with async_session_factory() as db:
        user = User(
            email=f"fresh-{uuid.uuid4().hex[:8]}@test.local",
            password_hash=hash_password("x"),
            expo_push_token="ExponentPushToken[fresh]",
        )
        db.add(user)
        await db.flush()
        db.add(PushTicketRecord(ticket_id="tk-fresh", user_id=user.id))  # created_at = now
        await db.commit()
        user_id = user.id

    try:
        with patch.object(notify_users, "_push_client") as client:
            await notify_users._check_push_receipts()
            client.return_value.check_receipts.assert_not_called()
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(PushTicketRecord).where(PushTicketRecord.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()
