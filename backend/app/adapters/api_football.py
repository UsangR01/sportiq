from app.adapters.base import (
    DataSourceAdapter,
    FixturePayload,
    InjuryUpdate,
    OddsPayload,
    TeamStats,
)
from app.core.config import get_settings


class APIFootballAdapter(DataSourceAdapter):
    """Football fixtures, team stats, H2H, xG, form, standings (TDD §2.2). Not yet
    implemented — needs API_FOOTBALL_KEY (RapidAPI) and a real HTTP client."""

    def __init__(self) -> None:
        self._api_key = get_settings().api_football_key

    async def fetch_odds(self, fixture_ids: list[str]) -> list[OddsPayload]:
        raise NotImplementedError("API-Football does not provide odds — use TheRundownAdapter")

    async def fetch_fixtures(
        self, sport: str, league: str, days_ahead: int
    ) -> list[FixturePayload]:
        raise NotImplementedError("API-Football fixture fetch not yet implemented")

    async def fetch_team_stats(self, team_id: str, n_matches: int) -> TeamStats:
        raise NotImplementedError("API-Football team stats fetch not yet implemented")

    async def fetch_injuries(self, sport: str) -> list[InjuryUpdate]:
        raise NotImplementedError("API-Football does not provide injury data")
