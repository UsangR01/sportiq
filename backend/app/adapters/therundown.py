from datetime import UTC, datetime, timedelta

import httpx

from app.adapters.base import (
    DataSourceAdapter,
    FixturePayload,
    InjuryUpdate,
    OddsPayload,
    TeamStats,
)
from app.core.config import get_settings

# Confirmed via live research (see CLAUDE.md): RapidAPI host/auth headers, real sport_id
# values, and the fact that only some bookmakers return real prices on this plan.
BASE_URL = "https://therundown-therundown-v1.p.rapidapi.com"
RAPIDAPI_HOST = "therundown-therundown-v1.p.rapidapi.com"

# TheRundown's own sport_id, confirmed via GET /sports. NBA is one sport; each football
# league is its own sport_id (not "football" as one id) — so this is keyed by league slug
# first, falling back to sport slug for single-league sports like NBA.
_RUNDOWN_SPORT_IDS: dict[str, int] = {
    "nba": 4,
    # Football leagues (PRD 4.1 MVP scope) — recorded now even though no football Sport/
    # League rows are seeded yet, since these are confirmed real IDs and cheap to keep here.
    "epl": 11,
    "ligue1": 12,
    "bundesliga": 13,
    "laliga": 14,
    "seriea": 15,
}

# TheRundown returns a masked 0.0001 sentinel for markets a bookmaker hasn't priced (or that
# this plan's tier doesn't unlock for that book) — confirmed live: only 3 of ~15 affiliates
# (Bovada, Bodog, BetMGM) return real values on the current subscription; DraftKings,
# FanDuel, Pinnacle, etc. are masked. Treated as "no line from this book", not an error.
_MASKED_ODDS_SENTINEL = 0.0001


def _rundown_sport_id_for(sport_slug: str, league_slug: str) -> int:
    if league_slug in _RUNDOWN_SPORT_IDS:
        return _RUNDOWN_SPORT_IDS[league_slug]
    if sport_slug in _RUNDOWN_SPORT_IDS:
        return _RUNDOWN_SPORT_IDS[sport_slug]
    raise ValueError(
        f"No TheRundown sport_id mapping for sport={sport_slug!r} league={league_slug!r}"
    )


def _american_to_decimal(american: float | None) -> float | None:
    """TDD §2.3 says odds are "normalised to decimal format" at ingest — TheRundown returns
    American odds ("format":"American" in every line block)."""
    if american is None or american == _MASKED_ODDS_SENTINEL or american == 0:
        return None
    if american > 0:
        return round(1 + american / 100, 4)
    return round(1 + 100 / abs(american), 4)


def _map_event_to_odds_payloads(event: dict) -> list[OddsPayload]:
    """Pure, network/DB-free mapping — kept separate so it's directly unit-testable against a
    recorded sample response. Maps the moneyline ("h2h") market and, since this feature was
    added, the "total" (Over/Under goals) market too — confirmed live (see CLAUDE.md) that
    TheRundown's raw `total` block carries real lines/prices, not just a masked sentinel, for
    the same unlocked affiliates as moneyline. `spread` still isn't mapped: nothing downstream
    consumes a point-spread market. TheRundown has no double-chance or corners market at all
    (its line blocks only ever have moneyline/spread/total keys) — those two markets can only
    ever come from API-Football, for the leagues it covers odds for."""
    normalized_by_side = {t.get("is_home"): t for t in event.get("teams_normalized", [])}
    home_abbr = normalized_by_side.get(True, {}).get("abbreviation")
    away_abbr = normalized_by_side.get(False, {}).get("abbreviation")
    kickoff_utc = datetime.fromisoformat(event["event_date"].replace("Z", "+00:00"))

    payloads: list[OddsPayload] = []
    for line in event.get("lines", {}).values():
        affiliate = line.get("affiliate", {}).get("affiliate_name", "unknown")
        moneyline = line.get("moneyline")
        if moneyline:
            home_odds = _american_to_decimal(moneyline.get("moneyline_home"))
            away_odds = _american_to_decimal(moneyline.get("moneyline_away"))
            if home_odds is not None or away_odds is not None:
                date_updated = moneyline.get("date_updated")
                updated_at = (
                    datetime.fromisoformat(date_updated.replace("Z", "+00:00"))
                    if date_updated
                    else datetime.now(UTC)
                )
                payloads.append(
                    OddsPayload(
                        fixture_external_id=event["event_id"],
                        bookmaker=affiliate,
                        market="h2h",
                        home_odds=home_odds,
                        draw_odds=_american_to_decimal(moneyline.get("moneyline_draw")),
                        away_odds=away_odds,
                        updated_at=updated_at,
                        home_team_short_name=home_abbr,
                        away_team_short_name=away_abbr,
                        kickoff_utc=kickoff_utc,
                    )
                )

        total = line.get("total")
        if total:
            total_line = total.get("total_over")  # total_over == total_under, same line value
            over_odds = _american_to_decimal(total.get("total_over_money"))
            under_odds = _american_to_decimal(total.get("total_under_money"))
            if total_line not in (None, _MASKED_ODDS_SENTINEL) and (
                over_odds is not None or under_odds is not None
            ):
                date_updated = total.get("date_updated")
                updated_at = (
                    datetime.fromisoformat(date_updated.replace("Z", "+00:00"))
                    if date_updated
                    else datetime.now(UTC)
                )
                payloads.append(
                    OddsPayload(
                        fixture_external_id=event["event_id"],
                        bookmaker=affiliate,
                        market="total",
                        home_odds=None,
                        draw_odds=None,
                        away_odds=None,
                        updated_at=updated_at,
                        home_team_short_name=home_abbr,
                        away_team_short_name=away_abbr,
                        kickoff_utc=kickoff_utc,
                        line=float(total_line),
                        over_odds=over_odds,
                        under_odds=under_odds,
                    )
                )
    return payloads


class TheRundownAdapter(DataSourceAdapter):
    """Odds + scores, all sports (TDD §2.2) — the odds adapter is always this one,
    regardless of sport (TDD §6.2). fetch_odds is real (below); fetch_fixtures/
    fetch_team_stats/fetch_injuries remain unimplemented — out of scope for the current task
    (NBA fixtures/stats come from BallDontLie, per TDD §2.2/§7)."""

    def __init__(self) -> None:
        self._api_key = get_settings().therundown_api_key

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"x-rapidapi-host": RAPIDAPI_HOST, "x-rapidapi-key": self._api_key},
            timeout=10.0,
        )

    async def fetch_odds(self, sport: str, league: str, days_ahead: int) -> list[OddsPayload]:
        rundown_sport_id = _rundown_sport_id_for(sport, league)
        now = datetime.now(UTC)
        payloads: list[OddsPayload] = []

        async with self._client() as client:
            for offset in range(days_ahead + 1):
                date_str = (now + timedelta(days=offset)).date().isoformat()
                response = await client.get(
                    f"/sports/{rundown_sport_id}/events/{date_str}", params={"include": "scores"}
                )
                response.raise_for_status()
                for event in response.json().get("events", []):
                    payloads.extend(_map_event_to_odds_payloads(event))

        return payloads

    async def fetch_fixtures(
        self, sport: str, league: str, days_ahead: int, days_back: int = 0
    ) -> list[FixturePayload]:
        raise NotImplementedError("TheRundown fixture/score fetch not yet implemented")

    async def fetch_team_stats(
        self, team_id: str, n_matches: int, league: str | None = None
    ) -> TeamStats:
        raise NotImplementedError("TheRundown does not provide team stats")

    async def fetch_injuries(self, sport: str) -> list[InjuryUpdate]:
        raise NotImplementedError("TheRundown does not provide injury data")
