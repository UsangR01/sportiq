from app.adapters.base import (
    DataSourceAdapter,
    FixturePayload,
    InjuryUpdate,
    OddsPayload,
    TeamStats,
)
from app.core.config import get_settings


class TheRundownAdapter(DataSourceAdapter):
    """Odds + scores, all sports (TDD §2.2). Not yet implemented — needs THERUNDOWN_API_KEY
    and a real HTTP client; see ingest_odds.py / ingest_live_scores.py for call sites."""

    def __init__(self) -> None:
        self._api_key = get_settings().therundown_api_key

    async def fetch_odds(self, fixture_ids: list[str]) -> list[OddsPayload]:
        raise NotImplementedError("TheRundown odds fetch not yet implemented")

    async def fetch_fixtures(
        self, sport: str, league: str, days_ahead: int
    ) -> list[FixturePayload]:
        raise NotImplementedError("TheRundown fixture/score fetch not yet implemented")

    async def fetch_team_stats(self, team_id: str, n_matches: int) -> TeamStats:
        raise NotImplementedError("TheRundown does not provide team stats")

    async def fetch_injuries(self, sport: str) -> list[InjuryUpdate]:
        raise NotImplementedError("TheRundown does not provide injury data")
