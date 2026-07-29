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
    TOTAL = "total"  # over/under goals — home_odds/draw_odds/away_odds unused, see below
    # Both added for the football multi-market feature (double chance, O/U goals, O/U
    # corners) — see CLAUDE.md. DOUBLE_CHANCE is a genuine 2-way market (home_odds = Home-or-
    # Draw price, away_odds = Away-or-Draw price, draw_odds unused) so it reuses the existing
    # h2h-shaped columns rather than adding new ones.
    DOUBLE_CHANCE = "double_chance"
    CORNERS_TOTAL = "corners_total"


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
    # TOTAL/CORNERS_TOTAL only: the numeric line (e.g. 2.5 goals, 9.5 corners) and its two
    # prices. Null for h2h/double_chance, which have no line.
    line: Mapped[float | None] = mapped_column(Float, nullable=True)
    over_odds: Mapped[float | None] = mapped_column(Float, nullable=True)
    under_odds: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
