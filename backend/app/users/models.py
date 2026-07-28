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
