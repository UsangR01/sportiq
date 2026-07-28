from app.adapters.base import (
    DataSourceAdapter,
    FixturePayload,
    InjuryUpdate,
    OddsPayload,
    TeamStats,
)
from app.core.config import get_settings


class BallDontLieAdapter(DataSourceAdapter):
    """NBA live production stats: games, box scores, advanced stats (net rating, pace), season
    averages, standings, and injuries (as a RotoWire fallback). TDD §2.1/§2.2/§7: this is the
    sole live NBA stats source — nba_api/stats.nba.com is offline-training-only and must never
    be called from a production worker (it blocks cloud-provider IPs). Not yet implemented —
    needs BALLDONTLIE_API_KEY and a real HTTP client."""

    def __init__(self) -> None:
        self._api_key = get_settings().balldontlie_api_key

    async def fetch_odds(self, fixture_ids: list[str]) -> list[OddsPayload]:
        raise NotImplementedError("BallDontLie does not provide odds — use TheRundownAdapter")

    async def fetch_fixtures(
        self, sport: str, league: str, days_ahead: int
    ) -> list[FixturePayload]:
        raise NotImplementedError("BallDontLie fixture fetch not yet implemented")

    async def fetch_team_stats(self, team_id: str, n_matches: int) -> TeamStats:
        raise NotImplementedError("BallDontLie team stats fetch not yet implemented")

    async def fetch_injuries(self, sport: str) -> list[InjuryUpdate]:
        """Fallback path when ROTOWIRE_API_KEY is absent (TDD §2.3). Less real-time than
        RotoWire — no GTD → OUT transition alerts — and the re-inference trigger stays
        disabled when this path is used."""
        raise NotImplementedError("BallDontLie injury fetch not yet implemented")
