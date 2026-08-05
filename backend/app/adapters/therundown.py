import asyncio
from datetime import UTC, date, datetime, timedelta

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

MAX_RETRIES = 5
# Proactive pacing between requests. CLAUDE.md's own live finding: this provider's 429s can
# escalate into spurious 401s under sustained burst load, so retrying alone isn't enough - the
# burst has to be spread out in the first place. 2s keeps a full multi-league ingest run
# comfortably inside the 5-minute schedule interval while staying well off the burst threshold.
ODDS_REQUEST_DELAY_SECONDS = 2.0

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
    "mls": 10,  # confirmed live via GET /sports — real coverage, unlike Scottish Prem/CSL below
    # No "scottish_prem" or "csl" entry: confirmed live via GET /sports that this subscription's
    # sport list has no Scotland or China league entry at all (same real gap as Brasileirão's
    # missing Brazil entry) — _rundown_sport_id_for raises ValueError for these, caught
    # per-adapter in ingest_odds.py so it never blocks the OTHER real odds source
    # (API-Football, which does have real coverage for all three — see api_football.py).
    #
    # Tennis. Confirmed live: 25 real ATP events on a single day, with up to ELEVEN affiliates
    # returning unmasked prices (Pinnacle, FanDuel, Bodog among them) — materially better than
    # football's 3-of-15. This is the only odds source tennis has ever had; before it, every
    # ATP pick was probability-only with no EV ranking possible.
    "atp": 38,
    "wta": 39,
}

# TheRundown returns a masked 0.0001 sentinel for markets a bookmaker hasn't priced (or that
# this plan's tier doesn't unlock for that book) — confirmed live: only 3 of ~15 affiliates
# (Bovada, Bodog, BetMGM) return real values on the current subscription; DraftKings,
# FanDuel, Pinnacle, etc. are masked. Treated as "no line from this book", not an error.
_MASKED_ODDS_SENTINEL = 0.0001


def _cross_provider_key(team: dict, key_on_full_name: bool = False) -> str | None:
    """The string used to match a TheRundown event onto a Fixture we already have.

    key_on_full_name is set for tennis only (see _map_event_to_odds_payloads). Everything else
    keeps using the abbreviation, which is what every existing team-sport match relies on."""
    if key_on_full_name:
        return team.get("name") or team.get("abbreviation") or None
    return team.get("abbreviation") or team.get("name") or None


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


def _map_event_to_odds_payloads(
    event: dict, *, key_on_full_name: bool = False
) -> list[OddsPayload]:
    """Pure, network/DB-free mapping — kept separate so it's directly unit-testable against a
    recorded sample response. Maps the moneyline ("h2h") market and, since this feature was
    added, the "total" (Over/Under goals) market too — confirmed live (see CLAUDE.md) that
    TheRundown's raw `total` block carries real lines/prices, not just a masked sentinel, for
    the same unlocked affiliates as moneyline. `spread` still isn't mapped: nothing downstream
    consumes a point-spread market. TheRundown has no double-chance or corners market at all
    (its line blocks only ever have moneyline/spread/total keys) — those two markets can only
    ever come from API-Football, for the leagues it covers odds for."""
    normalized_by_side = {t.get("is_home"): t for t in event.get("teams_normalized", [])}
    # Which field is the cross-provider key depends on the sport, and getting this wrong
    # breaks matching silently. For team sports it is the abbreviation: TheRundown says
    # name="Detroit" / abbreviation="DET" and BallDontLie stores "DET", so keying on the name
    # would match nothing. For tennis a "team" is one person: TheRundown abbreviates
    # "F. Cobolli" while BallDontLie stores "Flavio Cobolli", and an initial cannot be
    # recovered from a first name, so only the full name can match there.
    home_abbr = _cross_provider_key(normalized_by_side.get(True, {}), key_on_full_name)
    away_abbr = _cross_provider_key(normalized_by_side.get(False, {}), key_on_full_name)
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

    async def _get_with_retry(self, client: httpx.AsyncClient, path: str, params: dict):
        """Retry-on-429 with backoff, honouring Retry-After.

        Added after a real, two-day production outage: odds ingestion silently stopped writing
        anything on 2026-07-31, leaving only 6 of 51 upcoming football fixtures with any odds
        at all. This adapter had NO retry and NO pacing, while `ingest_odds` runs every 5
        minutes and fans out (days_ahead + 1) requests per league across ~7 leagues — a burst
        that reliably trips TheRundown's limit. A single 429 then raised straight out of
        fetch_odds and killed the whole task, every cycle, indefinitely.

        The knock-on effect was worse than missing prices: with no odds, expected-value ranking
        and the min_odds filter both degrade to probability-only behaviour, which is precisely
        the "every pick is UNDER 3.5" symptom that looked like a modelling problem.

        CLAUDE.md already documented that this provider's 429s can escalate into spurious 401s
        under sustained load, so ODDS_REQUEST_DELAY_SECONDS paces requests proactively rather
        than relying on retries alone."""
        response = None
        for attempt in range(MAX_RETRIES):
            response = await client.get(path, params=params)
            if response.status_code == 429 and attempt < MAX_RETRIES - 1:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else min(2**attempt, 30)
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            return response
        response.raise_for_status()
        return response

    async def fetch_odds(
        self,
        sport: str,
        league: str,
        days_ahead: int,
        dates: list[date] | None = None,
    ) -> list[OddsPayload]:
        rundown_sport_id = _rundown_sport_id_for(sport, league)
        now = datetime.now(UTC)
        payloads: list[OddsPayload] = []

        # Only the dates the caller actually needs, when it knows them. This endpoint is
        # one-request-per-date, so blindly walking every day in the lookahead window spends a
        # request on dates with no fixtures at all — which is most of them for most leagues.
        # That waste is what exhausted a 1,000-request MONTHLY quota in roughly 90 minutes and
        # left football with no odds for weeks (see ingest_odds.py:_dates_with_fixtures).
        request_dates = (
            [d.isoformat() for d in dates]
            if dates is not None
            else [
                (now + timedelta(days=offset)).date().isoformat()
                for offset in range(days_ahead + 1)
            ]
        )

        async with self._client() as client:
            for date_str in request_dates:
                response = await self._get_with_retry(
                    client,
                    f"/sports/{rundown_sport_id}/events/{date_str}",
                    {"include": "scores"},
                )
                for event in response.json().get("events", []):
                    payloads.extend(
                        _map_event_to_odds_payloads(event, key_on_full_name=sport == "tennis")
                    )
                await asyncio.sleep(ODDS_REQUEST_DELAY_SECONDS)

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
