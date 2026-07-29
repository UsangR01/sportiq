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

# Real league IDs, confirmed live via GET /leagues?name=X&country=Y — must match
# app/adapters/therundown.py's _RUNDOWN_SPORT_IDS keys exactly so odds ingestion resolves to
# the same League.slug rows.
LEAGUE_IDS: dict[str, int] = {
    "epl": 39,
    "ligue1": 61,
    "bundesliga": 78,
    "laliga": 140,
    "seriea": 135,
}

INJURY_LOOKAHEAD_DAYS = 3  # how far ahead fetch_injuries looks for fixtures to check dates for


def _current_football_season(now: datetime | None = None) -> int:
    """API-Football labels a season by the year it starts (e.g. 2025 for "2025-26", which
    runs roughly Aug 2025 - May 2026) — same start-year convention as BallDontLie's NBA
    seasons, just on a European Aug-May calendar instead of Oct-Jun."""
    now = now or datetime.now(UTC)
    return now.year if now.month >= 7 else now.year - 1


def _map_status(short_status: str) -> str:
    """API-Football's fixture.status.short: "NS" (not started) before kickoff, "FT"/"AET"/
    "PEN" once finished, anything else (1H/HT/2H/ET/BT/P/SUSP/INT/LIVE) is in progress."""
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
    """Football fixtures, team stats, injuries (TDD §2.2) — real Pro-tier implementation
    (confirmed live: 7500 req/day, active until 2026-08-29, see CLAUDE.md). fetch_odds stays
    unimplemented by design: odds is always TheRundown regardless of sport (TDD §6.2), and
    API-Football's own /teams/statistics response confirms odds:false at every tier tested."""

    def __init__(self) -> None:
        self._api_key = get_settings().api_football_key

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=BASE_URL, headers={"x-apisports-key": self._api_key}, timeout=15.0
        )

    async def fetch_odds(self, sport: str, league: str, days_ahead: int) -> list[OddsPayload]:
        raise NotImplementedError("API-Football does not provide odds — use TheRundownAdapter")

    async def fetch_fixtures(
        self, sport: str, league: str, days_ahead: int
    ) -> list[FixturePayload]:
        league_id = LEAGUE_IDS.get(league)
        if league_id is None:
            raise ValueError(f"No API-Football league id mapping for league={league!r}")

        now = datetime.now(UTC)
        params = {
            "league": league_id,
            "season": _current_football_season(now),
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
            "season": _current_football_season(),
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
        upcoming fixture across all 5 leagues in the next INJURY_LOOKAHEAD_DAYS days; genuinely
        returns nothing for dates too far out (confirmed live: real injury news doesn't exist
        that early — see CLAUDE.md), which is correct, not a bug."""
        now = datetime.now(UTC)
        season = _current_football_season(now)
        updates: list[InjuryUpdate] = []

        async with self._client() as client:
            for league_id in LEAGUE_IDS.values():
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
            params={"h2h": f"{home_external_id}-{away_external_id}", "last": H2H_LOOKBACK_MEETINGS},
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
