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

# Confirmed via live research (see CLAUDE.md): the real, working base host and auth header —
# NOT RapidAPI, despite TDD §2.2 saying "API-Football (via RapidAPI)". Auth is a raw key value
# under x-apisports-key, no Bearer/host header needed on this host.
BASE_URL = "https://v3.football.api-sports.io"

# Real league IDs, confirmed live via GET /leagues?name=X&country=Y — the 5 European leagues
# must match app/adapters/therundown.py's _RUNDOWN_SPORT_IDS keys exactly so odds ingestion
# resolves to the same League.slug rows. "brasileirao" is a deliberate exception: confirmed
# live (see CLAUDE.md) that TheRundown's own /sports list has no Brazil-league entry at all —
# odds ingestion for this one league gracefully no-ops (see ingest_odds.py) rather than
# raising, since fixtures/stats/injuries/predictions don't depend on odds coverage existing.
LEAGUE_IDS: dict[str, int] = {
    "epl": 39,
    "ligue1": 61,
    "bundesliga": 78,
    "laliga": 140,
    "seriea": 135,
    "brasileirao": 71,  # Brazil Serie A ("Brasileirão Betano") — confirmed live: id 71
}

# Leagues whose season runs on the calendar year (Jan-Dec) rather than the European Aug-May
# convention — confirmed live for Brasileirão: current season "2026" runs 2026-01-28 to
# 2026-12-02. Using the European convention here would compute the WRONG season year for
# most of the year (e.g. any month before July would look back a full year too far).
CALENDAR_YEAR_SEASON_LEAGUES = {"brasileirao"}

INJURY_LOOKAHEAD_DAYS = 3  # how far ahead fetch_injuries looks for fixtures to check dates for


def _current_football_season(league: str, now: datetime | None = None) -> int:
    """API-Football labels a season by the year it starts. For the 5 European leagues that's
    Aug-May (e.g. 2025 for "2025-26") — same start-year convention as BallDontLie's NBA
    seasons, just on a different calendar. Brasileirão instead runs Jan-Dec of a single
    calendar year (see CALENDAR_YEAR_SEASON_LEAGUES) — a real distinction found live while
    adding this league, not something either convention can be assumed from the other.
    """
    now = now or datetime.now(UTC)
    if league in CALENDAR_YEAR_SEASON_LEAGUES:
        return now.year
    return now.year if now.month >= 7 else now.year - 1


def _map_status(short_status: str) -> str:
    """API-Football's fixture.status.short: "NS" (not started) before kickoff, "FT"/"AET"/
    "PEN" once finished, anything else (1H/HT/2H/ET/BT/P/SUSP/INT/LIVE) is in progress.
    """
    if short_status == "NS":
        return "scheduled"
    if short_status in ("FT", "AET", "PEN"):
        return "completed"
    return "live"


def _map_fixture_to_payload(fixture: dict, league_slug: str) -> FixturePayload:
    """Pure, network/DB-free mapping — kept separate so it's directly unit-testable against a
    recorded sample response, mirroring app/adapters/balldontlie.py's pattern."""
    fx = fixture["fixture"]
    league = fixture["league"]
    teams = fixture["teams"]
    return FixturePayload(
        external_id=str(fx["id"]),
        league_external_id=league_slug,
        home_team_external_id=str(teams["home"]["id"]),
        away_team_external_id=str(teams["away"]["id"]),
        kickoff_utc=datetime.fromisoformat(fx["date"].replace("Z", "+00:00")),
        season=str(league["season"]),
        home_team_name=teams["home"].get("name"),
        away_team_name=teams["away"].get("name"),
        # API-Football has no short "abbreviation" field like BallDontLie's — only a full
        # team name and a separate logo URL. TheRundown's teams_normalized.abbreviation is
        # what fixture-matching (find_fixture_by_abbreviations_and_time) actually needs to
        # match against; API-Football's own name is used as short_name until proven otherwise
        # — same "best available field" tradeoff noted for other cross-provider joins.
        home_team_short_name=teams["home"].get("name"),
        away_team_short_name=teams["away"].get("name"),
        status=_map_status(fx["status"]["short"]),
    )


def _parse_form_points(form: str | None) -> float | None:
    """Converts API-Football's real "WWDLW..." form string (most-recent last) into a
    3/1/0-points-per-game average — the actual convention form_pts_5's name always implied
    but NBA's own win/loss-only implementation never used (NBA has no draws)."""
    if not form:
        return None
    points = {"W": 3, "D": 1, "L": 0}
    values = [points[c] for c in form if c in points]
    if not values:
        return None
    return sum(values) / len(values)


def _compute_team_stats(team_external_id: str, stats: dict) -> TeamStats:
    """Derives everything real /teams/statistics provides (form, goals-average, home/away
    win rate) — confirmed live (see CLAUDE.md): no elo_rating/xg fields exist in this
    response at any tier, so those stay None, same honest-gap pattern as NBA's pace
    differential."""
    fixtures = stats.get("fixtures", {})
    goals = stats.get("goals", {})

    played_home = fixtures.get("played", {}).get("home") or 0
    played_away = fixtures.get("played", {}).get("away") or 0
    wins_home = fixtures.get("wins", {}).get("home") or 0
    wins_away = fixtures.get("wins", {}).get("away") or 0

    home_win_rate = (wins_home / played_home) if played_home else None
    away_win_rate = (wins_away / played_away) if played_away else None

    # Season-wide averages, NOT a last-5 rolling window — /teams/statistics has no per-match
    # breakdown to compute a true rolling average from at serving time. A real, documented
    # train/serve divergence from app/models_ml/football_features.py's training-time
    # attack_str/defence_str (computed from an exact last-5-match window over the collected
    # game log) — same spirit as NBA's home_court_indicator always being a constant 1.0,
    # flagged here rather than silently accepted.
    #
    # Confirmed live: before a season's first match, API-Football returns "0.0" (a string,
    # not null) for these averages rather than omitting them — treated as genuinely missing
    # (None), not a real "zero goals" signal, matching this codebase's "never fabricate a
    # neutral value" convention (see app/models_ml/nba_features.py's own docstring).
    has_played = (played_home + played_away) > 0
    attack_str_raw = goals.get("for", {}).get("average", {}).get("total") if has_played else None
    defence_str_raw = (
        goals.get("against", {}).get("average", {}).get("total") if has_played else None
    )

    return TeamStats(
        team_external_id=team_external_id,
        elo_rating=None,
        attack_str=float(attack_str_raw) if attack_str_raw is not None else None,
        defence_str=float(defence_str_raw) if defence_str_raw is not None else None,
        form_pts_5=_parse_form_points(stats.get("form")),
        xg_for_5=None,
        xg_against_5=None,
        days_since_last_match=None,  # not derivable from this aggregate-only endpoint
        home_win_rate=home_win_rate,
        away_win_rate=away_win_rate,
        season_point_diff=None,
    )


MATCH_WINNER_BET_NAME = "Match Winner"  # API-Football's own name for the h2h/moneyline market


def _map_odds_response_to_payloads(row: dict) -> list[OddsPayload]:
    """Pure, network/DB-free mapping — one OddsPayload per bookmaker, mirroring
    app/adapters/therundown.py:_map_event_to_odds_payloads's shape. Only the "Match Winner"
    bet is mapped (home/draw/away) — same scope decision as TheRundown's h2h-only ingestion;
    the dozen other bet types this endpoint returns (Asian handicap, Over/Under, ...) don't
    fit odds.home_odds/.draw_odds/.away_odds and nothing downstream consumes them.

    Odds are already real decimals here — unlike TheRundown, no American-to-decimal
    conversion is needed (confirmed live, see CLAUDE.md).

    fixture_external_id is API-Football's own fixture id — the SAME id space as
    Fixture.external_id for any league whose fixtures also come from API-Football (every
    football league, currently). That means odds ingestion can match directly on
    Fixture.external_id instead of the fuzzy team-abbreviation-plus-kickoff-time join
    TheRundown-sourced odds need (see app/workers/ingest_odds.py:_resolve_fixture) — a real
    simplification unique to same-provider fixtures+odds, not something to generalise
    to TheRundown's payloads."""
    fixture_id = str(row["fixture"]["id"])
    updated_at_raw = row.get("update")
    updated_at = (
        datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00"))
        if updated_at_raw
        else datetime.now(UTC)
    )

    payloads: list[OddsPayload] = []
    for bookmaker in row.get("bookmakers", []):
        match_winner = next(
            (b for b in bookmaker.get("bets", []) if b.get("name") == MATCH_WINNER_BET_NAME), None
        )
        if match_winner is None:
            continue

        odds_by_value = {v["value"]: v.get("odd") for v in match_winner.get("values", [])}
        home_odds = odds_by_value.get("Home")
        draw_odds = odds_by_value.get("Draw")
        away_odds = odds_by_value.get("Away")
        if home_odds is None and away_odds is None:
            continue  # this book hasn't posted this market for this fixture

        payloads.append(
            OddsPayload(
                fixture_external_id=fixture_id,
                bookmaker=bookmaker.get("name", "unknown"),
                market="h2h",
                home_odds=float(home_odds) if home_odds is not None else None,
                draw_odds=float(draw_odds) if draw_odds is not None else None,
                away_odds=float(away_odds) if away_odds is not None else None,
                updated_at=updated_at,
            )
        )
    return payloads


def _map_injury_to_update(injury: dict, source: str = "api_football") -> InjuryUpdate:
    """API-Football's /injuries rows are fixture-scoped ("Missing Fixture") rather than a
    RotoWire-style rolling current-status feed — every row it returns is, by construction,
    a confirmed absence, so every mapped row is InjuryStatus.OUT. No GTD/doubtful signal
    exists in this feed (see CLAUDE.md)."""
    player = injury["player"]
    team = injury["team"]
    return InjuryUpdate(
        player_external_id=str(player["id"]),
        team_external_id=str(team["id"]),
        player_name=player["name"],
        status="OUT",
        return_date=None,
        salary_rank=None,
        source=source,
    )


class APIFootballAdapter(DataSourceAdapter):
    """Football fixtures, team stats, injuries, and (per-league) odds — real Pro-tier
    implementation (confirmed live: 7500 req/day, active until 2026-08-29, see CLAUDE.md).

    fetch_odds is real but genuinely partial, not a universal TheRundown replacement:
    confirmed live via each league's /leagues coverage.odds flag that only Brasileirão has
    real odds coverage on this plan (13 real bookmakers incl. Bet365/Pinnacle/Betfair — richer
    than TheRundown's 3-of-~15-unmasked situation) — all 5 European leagues report
    coverage.odds=False, same as originally found pre-Pro-upgrade. The two providers are
    complementary per league, not redundant: TheRundown remains the only real odds source for
    the 5 European leagues (which it covers and API-Football doesn't); API-Football is now
    the only real odds source for Brasileirão (which TheRundown doesn't cover at all). See
    app/adapters/factory.py:get_odds_adapters and app/workers/ingest_odds.py for how both are
    queried and merged.
    """

    def __init__(self) -> None:
        self._api_key = get_settings().api_football_key

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=BASE_URL, headers={"x-apisports-key": self._api_key}, timeout=15.0
        )

    async def fetch_odds(self, sport: str, league: str, days_ahead: int) -> list[OddsPayload]:
        """Real for leagues API-Football covers odds for (currently only Brasileirão —
        see class docstring); genuinely empty (not an error) for a recognised league this
        plan has zero odds coverage for (confirmed live: EPL returns results=0, not a 4xx) —
        same "no line from this book" graceful-miss philosophy as TheRundown's masked-
        sentinel handling, just at the whole-league level instead of per-bookmaker.

        Queried per real upcoming fixture date (mirrors fetch_injuries's own
        dates-with-real-fixtures optimization) rather than blindly for every date in
        days_ahead, to avoid empty-date calls once more leagues gain odds coverage."""
        league_id = LEAGUE_IDS.get(league)
        if league_id is None:
            raise ValueError(f"No API-Football league id mapping for league={league!r}")

        now = datetime.now(UTC)
        season = _current_football_season(league, now)
        payloads: list[OddsPayload] = []

        async with self._client() as client:
            fixtures_response = await client.get(
                "/fixtures",
                params={
                    "league": league_id,
                    "season": season,
                    "from": now.date().isoformat(),
                    "to": (now + timedelta(days=days_ahead)).date().isoformat(),
                },
            )
            fixtures_response.raise_for_status()
            dates: set[date] = {
                datetime.fromisoformat(fx["fixture"]["date"].replace("Z", "+00:00")).date()
                for fx in fixtures_response.json().get("response", [])
            }

            for fixture_date in dates:
                odds_response = await client.get(
                    "/odds",
                    params={
                        "league": league_id,
                        "season": season,
                        "date": fixture_date.isoformat(),
                    },
                )
                odds_response.raise_for_status()
                for row in odds_response.json().get("response", []):
                    payloads.extend(_map_odds_response_to_payloads(row))

        return payloads

    async def fetch_fixtures(
        self, sport: str, league: str, days_ahead: int
    ) -> list[FixturePayload]:
        league_id = LEAGUE_IDS.get(league)
        if league_id is None:
            raise ValueError(f"No API-Football league id mapping for league={league!r}")

        now = datetime.now(UTC)
        params = {
            "league": league_id,
            "season": _current_football_season(league, now),
            "from": now.date().isoformat(),
            "to": (now + timedelta(days=days_ahead)).date().isoformat(),
        }
        async with self._client() as client:
            response = await client.get("/fixtures", params=params)
            response.raise_for_status()
        fixtures = response.json().get("response", [])
        return [_map_fixture_to_payload(fx, league) for fx in fixtures]

    async def fetch_team_stats(
        self, team_id: str, n_matches: int, league: str | None = None
    ) -> TeamStats:
        if league is None:
            raise ValueError("APIFootballAdapter.fetch_team_stats requires league (see base.py)")
        league_id = LEAGUE_IDS.get(league)
        if league_id is None:
            raise ValueError(f"No API-Football league id mapping for league={league!r}")

        params = {
            "league": league_id,
            "season": _current_football_season(league),
            "team": team_id,
        }
        async with self._client() as client:
            response = await client.get("/teams/statistics", params=params)
            response.raise_for_status()
        stats = response.json().get("response", {})
        return _compute_team_stats(team_id, stats)

    async def fetch_injuries(self, sport: str) -> list[InjuryUpdate]:
        """Bulk per-league-per-date query (confirmed live: /injuries?league=X&season=Y&date=Z
        works without needing fixture IDs upfront) — matches the ABC's existing bulk-fetch
        shape rather than iterating fixture IDs one at a time. Looks at every date with a real
        upcoming fixture across every league in LEAGUE_IDS in the next INJURY_LOOKAHEAD_DAYS
        days (season computed per-league — see _current_football_season); genuinely
        returns nothing for dates too far out (confirmed live: real injury news doesn't exist
        that early — see CLAUDE.md), which is correct, not a bug."""
        now = datetime.now(UTC)
        updates: list[InjuryUpdate] = []

        async with self._client() as client:
            for league_slug, league_id in LEAGUE_IDS.items():
                season = _current_football_season(league_slug, now)
                fixtures_response = await client.get(
                    "/fixtures",
                    params={
                        "league": league_id,
                        "season": season,
                        "from": now.date().isoformat(),
                        "to": (now + timedelta(days=INJURY_LOOKAHEAD_DAYS)).date().isoformat(),
                    },
                )
                fixtures_response.raise_for_status()
                dates: set[date] = {
                    datetime.fromisoformat(fx["fixture"]["date"].replace("Z", "+00:00")).date()
                    for fx in fixtures_response.json().get("response", [])
                }

                for fixture_date in dates:
                    injuries_response = await client.get(
                        "/injuries",
                        params={
                            "league": league_id,
                            "season": season,
                            "date": fixture_date.isoformat(),
                        },
                    )
                    injuries_response.raise_for_status()
                    for injury in injuries_response.json().get("response", []):
                        updates.append(_map_injury_to_update(injury))

        return updates


H2H_LOOKBACK_MEETINGS = 10  # API-Football's own default page size for this endpoint


async def fetch_h2h_win_rate(home_external_id: str, away_external_id: str) -> float | None:
    """Football-specific helper, not part of the DataSourceAdapter ABC — mirrors
    app/adapters/balldontlie.py:fetch_h2h_win_rate's role (fixture-specific, needs both
    teams, doesn't fit fetch_team_stats(team_id)'s shape). Unlike NBA, API-Football has a
    dedicated head-to-head endpoint (/fixtures/headtohead) rather than requiring a manual
    search through one team's own fixture history — simpler and a real, direct provider
    feature, not a workaround."""
    api_key = get_settings().api_football_key
    async with httpx.AsyncClient(
        base_url=BASE_URL, headers={"x-apisports-key": api_key}, timeout=15.0
    ) as client:
        response = await client.get(
            "/fixtures/headtohead",
            params={
                "h2h": f"{home_external_id}-{away_external_id}",
                "last": H2H_LOOKBACK_MEETINGS,
            },
        )
        response.raise_for_status()
    meetings = response.json().get("response", [])

    completed = [fx for fx in meetings if fx["fixture"]["status"]["short"] in ("FT", "AET", "PEN")]
    if not completed:
        return None

    def home_side_won(fx: dict) -> bool:
        home_goals = fx["goals"]["home"]
        away_goals = fx["goals"]["away"]
        if home_goals is None or away_goals is None:
            return False
        is_home_team_home_side = str(fx["teams"]["home"]["id"]) == home_external_id
        if is_home_team_home_side:
            return home_goals > away_goals
        return away_goals > home_goals

    return sum(1 for fx in completed if home_side_won(fx)) / len(completed)
