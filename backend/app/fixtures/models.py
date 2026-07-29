import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Enum, Float, ForeignKey, Integer, String, UniqueConstraint
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
    API_FOOTBALL = "api_football"


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
    # Real, persistent Elo state (app/models_ml/elo.py) — NULL until this team's first
    # Elo-tracked match completes (initializes to elo.INITIAL_ELO at that point), updated
    # incrementally exactly once per real completed fixture
    # (app/workers/ingest_fixtures.py:_maybe_settle_outcome). Distinct from
    # TeamFeatures.elo_rating, which is a point-in-time snapshot of this value taken at
    # ingest time for a specific upcoming fixture — this column is the live running value.
    elo_rating: Mapped[float | None] = mapped_column(Float, nullable=True)


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
    # Not in TDD §2.1's schema listing — added so ingest workers can upsert idempotently
    # against a provider's own fixture ID instead of matching on the internal UUID PK.
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # A separate column from external_id: the stats/fixtures provider (BallDontLie for NBA,
    # API-Football for football) and the odds provider (always TheRundown, per TDD §6.2) use
    # different ID spaces for the same real-world fixture. Populated on first successful
    # team+kickoff-time match (see app/fixtures/service.py:find_fixture_by_abbreviations_and_time)
    # so subsequent odds ingests can look the fixture up directly instead of re-matching.
    odds_provider_external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        UniqueConstraint("sport_id", "external_id", name="uq_fixtures_sport_external_id"),
        UniqueConstraint(
            "sport_id", "odds_provider_external_id", name="uq_fixtures_sport_odds_external_id"
        ),
    )

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
    # Not in TDD §2.1's schema listing — a season-long (not last-N) average point
    # differential, used as a "net rating" proxy by app/models_ml/nba_features.py.
    season_point_diff: Mapped[float | None] = mapped_column(Float, nullable=True)
    # TDD §3.3 "Big3/Top5 Key Player Availability Feature", Stage 2 — see
    # app/models_ml/nba_key_players.py:get_key_player_availability. Computed at ingest time
    # and again on the RotoWire re-inference trigger; only ever from player_injury_status,
    # never from box-score/lineup data (that would be target leakage — see PITFALL in TDD §3.3).
    key_players_available: Mapped[int | None] = mapped_column(Integer, nullable=True)
    key_players_per_combined: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Real consecutive-match streak feature (app/models_ml/football_features.py), sourced from
    # TeamStats.win_streak/.losing_streak (see app/adapters/api_football.py:_parse_streaks) —
    # None for sports/adapters with no streak source yet (NBA).
    win_streak: Mapped[float | None] = mapped_column(Float, nullable=True)
    losing_streak: Mapped[float | None] = mapped_column(Float, nullable=True)


class TeamKeyPlayer(Base):
    """Season-level Top 5 key players per team (Big3/Big5 source) — TDD §2.1/§3.3. Stage 1
    of the key-player availability feature: computed once per season from a trailing,
    sport-specific ranking metric (backward-looking, leakage-safe), completely independent of
    any single game's box score or who was actually available for a given game — that's
    Stage 2 (player_injury_status), computed separately. See app/models_ml/nba_key_players.py
    (NBA: WS/48-approximation ranking, PER-approximation combined metric) and
    app/models_ml/football_key_players.py (football: API-Football's own per-match `rating`
    stat used as both — see that module for why a single real provider metric suffices there
    where NBA had to hand-derive two separate approximations)."""

    __tablename__ = "team_key_players"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False
    )
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)
    player_rank: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-5
    # The stats provider's own player ID (nba_api for NBA, API-Football for football) — a
    # different ID space from player_injury_status.player_id (RotoWire/BallDontLie/
    # API-Football injuries). Stage 2 joins by player_name, not this column — see TDD §3.3.
    player_id: Mapped[str] = mapped_column(String(100), nullable=False)
    player_name: Mapped[str] = mapped_column(String(150), nullable=False)
    # Renamed from ws_48/per (NBA-only names) to genuinely sport-agnostic names — NBA keeps
    # writing its WS/48 approximation into rank_metric and PER approximation into
    # combined_metric; football writes the same real `games.rating` value into both.
    rank_metric: Mapped[float] = mapped_column(Float, nullable=False)
    combined_metric: Mapped[float] = mapped_column(Float, nullable=False)
    mpg: Mapped[float] = mapped_column(Float, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "team_id", "season_year", "player_rank", name="uq_team_key_players_team_season_rank"
        ),
    )


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
