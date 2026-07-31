from app.adapters.api_football import APIFootballAdapter
from app.adapters.balldontlie import BallDontLieAdapter
from app.adapters.balldontlie_tennis import BallDontLieTennisAdapter
from app.adapters.base import DataSourceAdapter
from app.adapters.rotowire import RotoWireAdapter
from app.adapters.sportsdataio import SportsDataIOAdapter
from app.adapters.therundown import TheRundownAdapter
from app.core.config import get_settings

# Maps sports.data_source_slug (TDD §6.2) to its primary stats/fixtures adapter.
_STATS_ADAPTERS: dict[str, type[DataSourceAdapter]] = {
    "football": APIFootballAdapter,
    "nba": BallDontLieAdapter,
    "tennis": BallDontLieTennisAdapter,
    "nfl": SportsDataIOAdapter,
    "nhl": SportsDataIOAdapter,
    "mlb": SportsDataIOAdapter,
}

# No "tennis" entry in _INJURY_ADAPTERS (no tennis injury feed at MVP — same as every
# non-NBA/football sport today) or _ODDS_ADAPTERS (odds are an explicit fast-follow; defaults
# to [TheRundownAdapter] below, which raises a per-adapter-isolated ValueError in
# ingest_odds.py until real tennis coverage is confirmed and mapped).

# Sport-specific, optional injury adapters. Absent entries mean "no injury feed for this sport".
_INJURY_ADAPTERS: dict[str, type[DataSourceAdapter]] = {
    "nba": RotoWireAdapter,
    "football": APIFootballAdapter,
}

# Odds adapters per sport, tried in order and merged — not "the odds adapter is always
# TheRundown" as TDD §6.2 originally assumed. Confirmed live (see CLAUDE.md): API-Football's
# real odds coverage is per-league, not per-sport — it covers Brasileirão (which TheRundown
# doesn't cover at all) but none of the 5 European leagues (which only TheRundown covers).
# Both are queried for football; a league only one of them covers just gets an empty list
# from the other, not an error (see app/workers/ingest_odds.py).
_ODDS_ADAPTERS: dict[str, list[type[DataSourceAdapter]]] = {
    "football": [TheRundownAdapter, APIFootballAdapter],
}


class AdapterFactory:
    """Resolves the odds, stats, and injury adapters for a sport at runtime (TDD §6.2).
    Stats/injury adapters are sport-specific; odds adapters are per-sport too now (see
    _ODDS_ADAPTERS) — TheRundown is still the only odds source for every sport besides
    football's per-league split."""

    @staticmethod
    def get_odds_adapters(sport_slug: str) -> list[DataSourceAdapter]:
        adapter_classes = _ODDS_ADAPTERS.get(sport_slug, [TheRundownAdapter])
        return [cls() for cls in adapter_classes]

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
