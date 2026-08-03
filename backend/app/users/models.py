import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, String, func
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
