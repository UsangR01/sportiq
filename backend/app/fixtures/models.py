import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class FixtureStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    COMPLETED = "completed"
    # A catch-all for every real "not actually happening as scheduled" provider status
    # (postponed/cancelled/abandoned/suspended/interrupted/TBD/awarded/walkover — see
    # app/adapters/api_football.py:_NOT_ACTUALLY_LIVE_STATUSES) — previously all silently
    # bucketed into SCHEDULED, which showed a normal market prediction/odds badge for a game
    # that isn't being played. Not modeling each of those 8 states individually is a deliberate
    # scope cut (per CLAUDE.md's TDD §2.1/§2.3 divergence note) — "postponed" is the
    # least-misleading single label for "this fixture is not resolving as originally listed".
    POSTPONED = "postponed"


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
    # Tennis-only: a tour (ATP/WTA) is one League row, but users need to know WHICH TOURNAMENT
    # a match belongs to — "ATP Tour" alone doesn't tell you what to look up in a betting app.
    # Denormalised onto the fixture rather than given its own table: these are display/grouping
    # values that arrive already embedded in every match response (no extra API call, no join
    # needed for the list feed), matching this schema's existing pragmatic-nullable-column
    # convention. `tournament_location` is a CITY ("Montreal"), not a country — the provider
    # exposes no country field, so the mobile flag maps city -> country itself.
    tournament_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tournament_surface: Mapped[str | None] = mapped_column(String(30), nullable=True)
    tournament_location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # When the competition finishes, so a PLACEHOLDER kickoff can be bounded.
    #
    # A tennis match with no scheduled_time inherits its TOURNAMENT'S START date, and
    # _roll_forward_stale_placeholders moves such a fixture to today once its day has passed --
    # correct for a real match still to be played, and unbounded for one that never will be.
    # Measured: a Toby Samuel v J.J. Wolf phantom stamped 11 Aug was still riding the feed on
    # 24 Aug, because BallDontLie keeps reporting it `scheduled` and never withdraws it, so
    # neither the vanished-fixture reconciliation nor the clock sweep can see it.
    #
    # The tournament's own close is the exact point after which the match cannot happen. Null
    # for football and basketball, and for tennis rows ingested before this existed.
    tournament_end_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # True when kickoff_utc was INFERRED rather than reported by the provider, so the client
    # can show "Time TBC" instead of a precise time we don't actually have. Tennis in practice:
    # ~95% of ATP matches carry no usable kickoff time, and falling back to the tournament's
    # start date gave every match in a 12-day draw the same timestamp — which showed wrong
    # times AND made matches appear on days they were never played. See
    # balldontlie_tennis.py:_match_kickoff_is_estimated.
    kickoff_is_estimated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Was this fixture WITHDRAWN by the provider rather than called off?
    #
    # POSTPONED is one shared bucket for every reason a match is not being played, which is the
    # right call for display — but it conflates two things a user experiences very differently.
    # A provider that explicitly reports PST/CANC is telling us a REAL scheduled match was
    # called off, and that is worth showing. A fixture that silently disappears from the
    # provider's list before its kickoff was, in the case this was built for, never a real
    # scheduled match at all: BallDontLie published a provisional Cincinnati draw, withdrew it,
    # and replaced it with different (and in several cases differently-paired) matches. Showing
    # 33 grey "POSTPONED" cards for matches that never existed buried the day's two genuine
    # picks underneath them.
    #
    # Set only by ingest_fixtures._reconcile_vanished_fixtures, and CLEARED whenever the
    # provider lists the fixture again — a withdrawn draw can be republished, and a fixture
    # that could never leave this flag would be hidden forever.
    withdrawn: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )

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
    # Tennis only (app/models_ml/tennis_features.py) — real ATP/WTA ranking points, sourced
    # from TeamStats.rank_points (app/adapters/balldontlie_tennis.py). None for every other
    # sport. Deliberately NOT storing a surface-specific win rate here: that signal is
    # fixture-specific (the CURRENT tournament's surface), not a per-team-per-run stat the way
    # every other TeamFeatures column is — it's fetched live in assemble_from_live_db instead,
    # the same way NBA's h2h_win_rate_home is a live call rather than a cached column.
    rank_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The same player's ranking POSITION (see TeamStats.rank_position). Nullable with no
    # backfill: predictions made before this column existed genuinely have no measurement,
    # and inventing one retroactively is exactly what feature_completeness exists to expose.
    rank_position: Mapped[float | None] = mapped_column(Float, nullable=True)


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
    # Football only — real corner-kick counts fetched exactly once, at fixture-settlement
    # time (app/workers/ingest_fixtures.py:_maybe_settle_outcome via
    # app/adapters/api_football.py:fetch_corner_stats), so the Over/Under corners market can
    # show a real win/loss verdict instead of staying permanently unverifiable. Null for NBA
    # (no corners concept) and for any fixture settled before this column existed.
    home_corners: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_corners: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # NULL for a normally-played-out result; "retired"/"walkover" for one that ended without
    # being played out (tennis-only in practice). Deliberately a column here rather than a new
    # FixtureStatus value: the match genuinely IS completed and has a real winner, so every
    # existing `status == COMPLETED` code path (settlement, retrodiction, feed filtering)
    # should keep treating it normally — this only changes how the RESULT is presented. The
    # mobile feed shows a neutral "RET" badge and withholds the win/loss verdict for these,
    # since bookmakers generally void bets on a retirement, so a tick would imply a payout the
    # user may never have received. See app/adapters/balldontlie_tennis.py:_match_result_type
    # for why this must be inferred from the score rather than read off match_status.
    result_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
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
