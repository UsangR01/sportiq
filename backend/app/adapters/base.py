from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class OddsPayload:
    # This is the ODDS provider's own event ID (TheRundown's, per TDD §6.2 — always TheRundown
    # regardless of sport) — a *different* ID space from a fixture's stats-provider
    # external_id (BallDontLie for NBA, API-Football for football). The two only get linked
    # by matching team abbreviations + kickoff time; see
    # app/fixtures/service.py:find_fixture_by_abbreviations_and_time.
    fixture_external_id: str
    bookmaker: str
    market: str  # "h2h" | "spread" | "total"
    home_odds: float | None
    draw_odds: float | None
    away_odds: float | None
    updated_at: datetime
    # Needed for fixture matching — not part of the original shape.
    home_team_short_name: str | None = None
    away_team_short_name: str | None = None
    kickoff_utc: datetime | None = None


@dataclass(frozen=True)
class FixturePayload:
    external_id: str
    league_external_id: str
    home_team_external_id: str
    away_team_external_id: str
    kickoff_utc: datetime
    season: str
    # Not part of the original shape — a first-seen team needs a display name to create its
    # Team row, and a provider that returns historical/completed fixtures (e.g. verifying
    # against a past date range) needs to say so, rather than every ingested fixture silently
    # defaulting to "scheduled" forever. Optional so adapters without this data can omit it.
    home_team_name: str | None = None
    away_team_name: str | None = None
    home_team_short_name: str | None = None
    away_team_short_name: str | None = None
    status: str = "scheduled"  # "scheduled" | "live" | "completed" — matches FixtureStatus


@dataclass(frozen=True)
class TeamStats:
    team_external_id: str
    elo_rating: float | None = None
    attack_str: float | None = None
    defence_str: float | None = None
    form_pts_5: float | None = None
    xg_for_5: float | None = None
    xg_against_5: float | None = None
    days_since_last_match: int | None = None
    home_win_rate: float | None = None
    away_win_rate: float | None = None


@dataclass(frozen=True)
class InjuryUpdate:
    player_external_id: str
    team_external_id: str
    player_name: str
    status: str  # "OUT" | "GTD" | "PROBABLE" | "ACTIVE"
    return_date: str | None
    salary_rank: int | None
    source: str  # "rotowire" | "balldontlie"


class DataSourceAdapter(ABC):
    """Every external provider is accessed only through this interface (TDD §2.2 KEY note).
    Ingest workers and routes must never call a provider's SDK/HTTP client directly."""

    @abstractmethod
    async def fetch_odds(self, sport: str, league: str, days_ahead: int) -> list[OddsPayload]:
        """Deliberately mirrors fetch_fixtures's shape, not a fixture_ids list as originally
        drafted: real odds providers (TheRundown) are queried by sport+date-range, not by IDs
        the caller already knows — those IDs live in a different provider's ID space
        entirely. The caller (ingest_odds.py) matches results to internal Fixture rows
        itself, the same way ingest_fixtures.py resolves teams via get_or_create_team."""
        ...

    @abstractmethod
    async def fetch_fixtures(
        self, sport: str, league: str, days_ahead: int
    ) -> list[FixturePayload]: ...

    @abstractmethod
    async def fetch_team_stats(self, team_id: str, n_matches: int) -> TeamStats: ...

    @abstractmethod
    async def fetch_injuries(self, sport: str) -> list[InjuryUpdate]: ...
