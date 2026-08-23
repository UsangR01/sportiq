import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OddsFormat(str, enum.Enum):
    DECIMAL = "decimal"
    FRACTIONAL = "fractional"
    AMERICAN = "american"


class ThemePreference(str, enum.Enum):
    """Light/dark/system appearance choice.

    SYSTEM is a real third state, not the absence of a value: it means "follow the OS", which
    is meaningfully different from a user who has explicitly chosen light. That's why the
    column is non-nullable with a SYSTEM default rather than nullable."""

    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(150), nullable=True)
    # TDD §5.4: "Token stored in users table alongside user_id" — not in the §2.1 schema listing.
    expo_push_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserPreference(Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    default_sport_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sports.id"), nullable=True
    )
    default_min_odds: Mapped[float] = mapped_column(Float, nullable=True)
    odds_format: Mapped[OddsFormat] = mapped_column(
        Enum(OddsFormat, name="odds_format"), default=OddsFormat.DECIMAL, nullable=False
    )
    # Display setting rather than a betting one, but it lives here so it follows the account
    # across devices. Guests keep theirs on-device only — a guest session is device-bound
    # anyway, so there is nothing to sync it to.
    theme_preference: Mapped[ThemePreference] = mapped_column(
        Enum(ThemePreference, name="theme_preference"),
        default=ThemePreference.SYSTEM,
        server_default="SYSTEM",
        nullable=False,
    )


class WatchlistItem(Base):
    """A fixture a user saved to follow (PRD PICK-07).

    Not in the TDD §2.1 schema listing — §5.4's T-60-minute kickoff reminder requires it, and
    app/workers/notify_users.py:notify_kickoff_reminder has been a NotImplementedError stub
    purely because there was nowhere to read "who wants telling about this fixture" from.

    Authenticated users only, deliberately. A guest session is device-bound state in Redis with
    a 24h TTL and no PII; a watchlist is durable and drives a push notification, which needs a
    push token that only an account carries. Guests get the existing soft auth prompt instead.
    """

    __tablename__ = "watchlist_items"
    __table_args__ = (
        # Saving the same fixture twice is a no-op, not a second row — the endpoint is
        # idempotent so a double tap on a flaky connection cannot produce two reminders.
        UniqueConstraint("user_id", "fixture_id", name="uq_watchlist_user_fixture"),
        # The reminder worker's own query shape: every watcher of one fixture.
        Index("ix_watchlist_fixture", "fixture_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fixture_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fixtures.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # THE PICK AS IT WAS SHOWN WHEN THIS USER SAVED IT.
    #
    # best_pick is recomputed on every request and never stored, so a saved fixture's card can
    # read differently every time it is opened -- a WNBA pick moved 59% -> 66% overnight and a
    # La Liga card went from "over 1.5" to a double chance, both reported as the app changing
    # its mind after the fact. The churn is mostly legitimate (books price late, and the market
    # feeds the model), so this freezes what THIS user saw at the moment they acted rather than
    # freezing the feed for everyone still deciding.
    #
    # Nullable with no backfill: a row saved before these existed has no honest record of what
    # was shown, and inventing one would be a fabricated receipt.
    saved_market: Mapped[str | None] = mapped_column(String(32), nullable=True)
    saved_selection: Mapped[str | None] = mapped_column(String(16), nullable=True)
    saved_line: Mapped[float | None] = mapped_column(Float, nullable=True)
    saved_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    saved_odds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Set once the T-60 reminder actually goes out, so a re-run of the scheduler cannot notify
    # the same user twice about the same fixture — the same idempotency guard style as
    # _maybe_settle_outcome. NULL means "not yet reminded".
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set once an AT RISK alert has gone out for this saved pick, so it fires ONCE per fixture
    # rather than once per poll. Live scores refresh every 5 minutes and a pick that goes
    # at-risk usually stays at-risk, so without this a single bad scoreline would send a user
    # roughly six notifications about the same match — the fastest way to have push disabled.
    #
    # Separate from reminded_at because they are different promises: one says a match is about
    # to start, the other that a pick has started going wrong. Deliberately NOT cleared if the
    # pick recovers; a second alert for a match that went at-risk, recovered and slipped again
    # is more noise than signal.
    at_risk_alerted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PushTicketRecord(Base):
    """One Expo push ticket, kept until its receipt has been checked.

    Expo delivery is two-stage and only the FIRST stage was ever observed here. publish()
    returns a TICKET, which means "accepted for delivery" — it is not a delivery. The actual
    outcome arrives later in a RECEIPT, and that is the only place errors like
    DeviceNotRegistered, MessageTooBig or InvalidCredentials appear. _send_push discarded the
    ticket id, so a misconfigured FCM credential would have failed every single send while every
    log line said the push had succeeded.

    Rows are deleted once checked: this is a work queue, not history. Keeping them would grow a
    table forever to record that a notification worked.
    """

    __tablename__ = "push_tickets"
    __table_args__ = (
        # The worker's only query: unchecked tickets old enough to have a receipt.
        Index("ix_push_tickets_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Expo's own ticket id. Nullable because a ticket can be accepted without one being
    # returned, in which case there is nothing to look up and the row is pointless — but the
    # send still happened, so it is recorded rather than silently dropped.
    ticket_id: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
