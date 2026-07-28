import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MatchResult(str, enum.Enum):
    HOME_WIN = "home_win"
    DRAW = "draw"
    AWAY_WIN = "away_win"


class Outcome(Base):
    __tablename__ = "outcomes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fixture_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fixtures.id"), nullable=False
    )
    home_score: Mapped[int] = mapped_column(Integer, nullable=False)
    away_score: Mapped[int] = mapped_column(Integer, nullable=False)
    result: Mapped[MatchResult] = mapped_column(
        Enum(MatchResult, name="match_result"), nullable=False
    )
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
