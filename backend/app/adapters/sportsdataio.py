from app.adapters.base import (
    DataSourceAdapter,
    FixturePayload,
    InjuryUpdate,
    OddsPayload,
    TeamStats,
)


class SportsDataIOAdapter(DataSourceAdapter):
    """NFL, NHL, MLB fixtures/stats/injuries — Phase 2 (TDD §2.2). Not yet implemented; no
    sport is configured to use this adapter at MVP (football + NBA only)."""

    async def fetch_odds(self, sport: str, league: str, days_ahead: int) -> list[OddsPayload]:
        raise NotImplementedError("SportsDataIO does not provide odds — use TheRundownAdapter")

    async def fetch_fixtures(
        self, sport: str, league: str, days_ahead: int, days_back: int = 0
    ) -> list[FixturePayload]:
        raise NotImplementedError("SportsDataIO adapter is Phase 2 — not yet implemented")

    async def fetch_team_stats(
        self, team_id: str, n_matches: int, league: str | None = None
    ) -> TeamStats:
        raise NotImplementedError("SportsDataIO adapter is Phase 2 — not yet implemented")

    async def fetch_injuries(self, sport: str) -> list[InjuryUpdate]:
        raise NotImplementedError("SportsDataIO adapter is Phase 2 — not yet implemented")
