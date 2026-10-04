from app.adapters.api_basketball import BASKETBALL_LEAGUE_IDS, APIBasketballAdapter
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
# non-NBA/football sport today). Tennis DOES now have an _ODDS_ADAPTERS entry — see below.

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
#
# Tennis pairs the two for a different reason than football does — not per-league coverage but
# per-MARKET. BallDontLie (GOAT tier) carries moneyline only, with no totals market at all,
# while TheRundown is the only source of game-totals lines. They also differ in join quality:
# BallDontLie is the tennis fixtures provider too, so its match_id matches Fixture.external_id
# directly, whereas TheRundown's prices need the team-name fuzzy join that has already
# produced both missed and inverted tennis prices. Both are queried; ingest merges them.
_ODDS_ADAPTERS: dict[str, list[type[DataSourceAdapter]]] = {
    "football": [TheRundownAdapter, APIFootballAdapter],
    "tennis": [TheRundownAdapter, BallDontLieTennisAdapter],
}


# LEAGUE-LEVEL OVERRIDES, because one sport can span two providers.
#
# _STATS_ADAPTERS above is keyed by SPORT, which held while each sport had one fixtures source.
# The European basketball competitions broke that: they live under Sport(slug="nba") -- so they
# share the NBA model, the sport tab and the base rates, which a measured transfer test says is
# right -- but BallDontLie has no European coverage at all, so routing them by sport would send
# every request to a provider that cannot answer it.
#
# A LEAGUE OVERRIDE RATHER THAN A SECOND SPORT ROW, deliberately. A separate Sport would need
# its own models_registry row pointing at the same artefact, its own base rates and its own tab,
# splitting basketball across two places in the app to work around a routing detail. The odds
# side already precedes this: _ODDS_ADAPTERS is per-sport precisely because coverage differs by
# LEAGUE within football.
_STATS_ADAPTERS_BY_LEAGUE: dict[str, type[DataSourceAdapter]] = {
    league: APIBasketballAdapter for league in BASKETBALL_LEAGUE_IDS
}

# Same split for odds: these leagues are priced by API-Basketball alone. TheRundown carries none
# of them -- its own /sports list is 36 entries with no European basketball entry at all -- so
# querying it would only ever raise the per-adapter ValueError ingest_odds already isolates.
_ODDS_ADAPTERS_BY_LEAGUE: dict[str, list[type[DataSourceAdapter]]] = {
    league: [APIBasketballAdapter] for league in BASKETBALL_LEAGUE_IDS
}


class AdapterFactory:
    """Resolves the odds, stats, and injury adapters for a sport at runtime (TDD §6.2).
    Stats/injury adapters are sport-specific; odds adapters are per-sport too now (see
    _ODDS_ADAPTERS) — TheRundown is still the only odds source for every sport besides
    football's per-league split."""

    @staticmethod
    def get_odds_adapters(
        sport_slug: str, league_slug: str | None = None
    ) -> list[DataSourceAdapter]:
        """league_slug is OPTIONAL so every existing caller keeps its exact behaviour; it only
        changes the answer for a league that has its own entry above."""
        if league_slug is not None and league_slug in _ODDS_ADAPTERS_BY_LEAGUE:
            return [cls() for cls in _ODDS_ADAPTERS_BY_LEAGUE[league_slug]]
        adapter_classes = _ODDS_ADAPTERS.get(sport_slug, [TheRundownAdapter])
        return [cls() for cls in adapter_classes]

    @staticmethod
    def get_stats_adapter(
        data_source_slug: str, league_slug: str | None = None
    ) -> DataSourceAdapter:
        """The league override wins where one exists -- see _STATS_ADAPTERS_BY_LEAGUE. Passing
        no league preserves the original per-sport behaviour exactly, which is what keeps every
        existing call site and test valid."""
        if league_slug is not None and league_slug in _STATS_ADAPTERS_BY_LEAGUE:
            return _STATS_ADAPTERS_BY_LEAGUE[league_slug]()
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
