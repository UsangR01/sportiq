from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class OddsPayload:
    fixture_external_id: str
    bookmaker: str
    market: str  # "h2h" | "spread" | "total"
    home_odds: float | None
    draw_odds: float | None
    away_odds: float | None
    updated_at: datetime


@dataclass(frozen=True)
class FixturePayload:
    external_id: str
    league_external_id: str
    home_team_external_id: str
    away_team_external_id: str
    kickoff_utc: datetime
    season: str


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
    async def fetch_odds(self, fixture_ids: list[str]) -> list[OddsPayload]: ...

    @abstractmethod
    async def fetch_fixtures(
        self, sport: str, league: str, days_ahead: int
    ) -> list[FixturePayload]: ...

    @abstractmethod
    async def fetch_team_stats(self, team_id: str, n_matches: int) -> TeamStats: ...

    @abstractmethod
    async def fetch_injuries(self, sport: str) -> list[InjuryUpdate]: ...
