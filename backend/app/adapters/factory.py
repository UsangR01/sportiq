from app.adapters.api_football import APIFootballAdapter
from app.adapters.balldontlie import BallDontLieAdapter
from app.adapters.base import DataSourceAdapter
from app.adapters.rotowire import RotoWireAdapter
from app.adapters.sportsdataio import SportsDataIOAdapter
from app.adapters.therundown import TheRundownAdapter
from app.core.config import get_settings

# Maps sports.data_source_slug (TDD §6.2) to its primary stats/fixtures adapter.
_STATS_ADAPTERS: dict[str, type[DataSourceAdapter]] = {
    "football": APIFootballAdapter,
    "nba": BallDontLieAdapter,
    "nfl": SportsDataIOAdapter,
    "nhl": SportsDataIOAdapter,
    "mlb": SportsDataIOAdapter,
}

# Sport-specific, optional injury adapters. Absent entries mean "no injury feed for this sport".
_INJURY_ADAPTERS: dict[str, type[DataSourceAdapter]] = {
    "nba": RotoWireAdapter,
}


class AdapterFactory:
    """Resolves the odds, stats, and injury adapters for a sport at runtime (TDD §6.2). The
    odds adapter is always TheRundown; stats/injury adapters are sport-specific."""

    @staticmethod
    def get_odds_adapter() -> DataSourceAdapter:
        return TheRundownAdapter()

    @staticmethod
    def get_stats_adapter(data_source_slug: str) -> DataSourceAdapter:
        adapter_cls = _STATS_ADAPTERS.get(data_source_slug)
        if adapter_cls is None:
            raise ValueError(
                f"No stats adapter registered for data_source_slug={data_source_slug!r}"
            )
        return adapter_cls()

    @staticmethod
    def get_injury_adapter(data_source_slug: str) -> DataSourceAdapter | None:
        """Returns None when the sport has no injury feed, or when the sport's primary feed
        (RotoWire for NBA) is unconfigured — callers should fall back accordingly, matching
        the ingest_injuries feature-flag logic (TDD §2.3)."""
        adapter_cls = _INJURY_ADAPTERS.get(data_source_slug)
        if adapter_cls is None:
            return None
        if adapter_cls is RotoWireAdapter and not get_settings().rotowire_api_key:
            return BallDontLieAdapter()
        return adapter_cls()
