import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Enum, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class FixtureStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    COMPLETED = "completed"


class InjuryStatus(str, enum.Enum):
    OUT = "OUT"
    GTD = "GTD"
    PROBABLE = "PROBABLE"
    ACTIVE = "ACTIVE"


class InjurySource(str, enum.Enum):
    ROTOWIRE = "rotowire"
    BALLDONTLIE = "balldontlie"


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sport_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sports.id"), nullable=False
    )
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    short_name: Mapped[str] = mapped_column(String(50), nullable=True)
    external_id: Mapped[str] = mapped_column(String(100), nullable=True)


class Fixture(Base):
    __tablename__ = "fixtures"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sport_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sports.id"), nullable=False
    )
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), nullable=False
    )
    home_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False
    )
    away_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False
    )
    kickoff_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[FixtureStatus] = mapped_column(
        Enum(FixtureStatus, name="fixture_status"), default=FixtureStatus.SCHEDULED, nullable=False
    )
    season: Mapped[str] = mapped_column(String(20), nullable=False)

    live_state: Mapped["FixtureLiveState | None"] = relationship(
        back_populates="fixture", uselist=False
    )


class TeamFeatures(Base):
    """Computed and stored at prediction time, not re-derived at query time (TDD §2.1)."""

    __tablename__ = "team_features"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False
    )
    fixture_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fixtures.id"), nullable=False
    )
    elo_rating: Mapped[float] = mapped_column(Float, nullable=True)
    attack_str: Mapped[float] = mapped_column(Float, nullable=True)
    defence_str: Mapped[float] = mapped_column(Float, nullable=True)
    form_pts_5: Mapped[float] = mapped_column(Float, nullable=True)
    xg_for_5: Mapped[float] = mapped_column(Float, nullable=True)
    xg_against_5: Mapped[float] = mapped_column(Float, nullable=True)
    days_since_last_match: Mapped[int] = mapped_column(Integer, nullable=True)
    home_win_rate: Mapped[float] = mapped_column(Float, nullable=True)
    away_win_rate: Mapped[float] = mapped_column(Float, nullable=True)


class FixtureLiveState(Base):
    """Separate upsert table so high-frequency live writes don't contaminate the core fixture
    record. The ingest worker upserts this every 5 minutes for live fixtures only (TDD §2.1)."""

    __tablename__ = "fixture_live_state"

    fixture_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fixtures.id"), primary_key=True
    )
    home_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    away_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    match_minute: Mapped[int] = mapped_column(Integer, nullable=True)
    period: Mapped[str] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    last_updated_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    fixture: Mapped["Fixture"] = relationship(back_populates="live_state")


class PlayerInjuryStatus(Base):
    """NBA (and other sports, Phase 2) player injury/lineup status. Sourced from RotoWire when
    ROTOWIRE_API_KEY is set, otherwise from the BallDontLie fallback (TDD §2.3)."""

    __tablename__ = "player_injury_status"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sport_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sports.id"), nullable=False
    )
    player_id: Mapped[str] = mapped_column(String(100), nullable=False)
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False
    )
    player_name: Mapped[str] = mapped_column(String(150), nullable=False)
    status: Mapped[InjuryStatus] = mapped_column(
        Enum(InjuryStatus, name="injury_status"), nullable=False
    )
    return_date: Mapped[date] = mapped_column(Date, nullable=True)
    salary_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    source: Mapped[InjurySource] = mapped_column(
        Enum(InjurySource, name="injury_source"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
