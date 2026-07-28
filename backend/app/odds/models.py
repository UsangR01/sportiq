import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OddsMarket(str, enum.Enum):
    H2H = "h2h"
    SPREAD = "spread"
    TOTAL = "total"


class Odds(Base):
    """No unique constraint on fixture+bookmaker: multiple historical snapshots are retained
    for line movement analysis and model training (TDD §2.1)."""

    __tablename__ = "odds"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fixture_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fixtures.id"), nullable=False
    )
    bookmaker: Mapped[str] = mapped_column(String(100), nullable=False)
    market: Mapped[OddsMarket] = mapped_column(Enum(OddsMarket, name="odds_market"), nullable=False)
    home_odds: Mapped[float] = mapped_column(Float, nullable=True)
    draw_odds: Mapped[float] = mapped_column(Float, nullable=True)
    away_odds: Mapped[float] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
