from app.adapters.base import (
    DataSourceAdapter,
    FixturePayload,
    InjuryUpdate,
    OddsPayload,
    TeamStats,
)
from app.core.config import get_settings


class RotoWireAdapter(DataSourceAdapter):
    """Real-time NBA injury status (OUT/GTD/Probable) and confirmed lineups, with priority
    scores (TDD §2.2). Paid API with no free tier (~$200/year, TDD §2.3/§7) — the caller
    (ingest_injuries worker) must check ROTOWIRE_API_KEY before constructing this adapter and
    fall back to BallDontLieAdapter when it's absent. Not yet implemented — needs a real
    HTTP client against api.rotowire.com."""

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.rotowire_api_key:
            raise RuntimeError("RotoWireAdapter constructed without ROTOWIRE_API_KEY set")
        self._api_key = settings.rotowire_api_key

    async def fetch_odds(self, fixture_ids: list[str]) -> list[OddsPayload]:
        raise NotImplementedError("RotoWire does not provide odds — use TheRundownAdapter")

    async def fetch_fixtures(
        self, sport: str, league: str, days_ahead: int
    ) -> list[FixturePayload]:
        raise NotImplementedError("RotoWire does not provide fixtures")

    async def fetch_team_stats(self, team_id: str, n_matches: int) -> TeamStats:
        raise NotImplementedError("RotoWire does not provide team stats")

    async def fetch_injuries(self, sport: str) -> list[InjuryUpdate]:
        """GET api.rotowire.com/Basketball/get-nba-injuries.php?key={key}&hours=0.5, filtered
        to InjuryStatus IN ('OUT','GTD') and Priority <= 2 (TDD §2.3)."""
        raise NotImplementedError("RotoWire injury fetch not yet implemented")
