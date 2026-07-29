import asyncio
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

# Confirmed via live research (see CLAUDE.md): this is the real, working base URL and auth
# header shape. Auth is the RAW key value — no "Bearer " prefix.
BASE_URL = "https://api.balldontlie.io/nba/v1"
MAX_PAGES = 20  # safety cap on cursor-pagination loops


def _current_nba_season(now: datetime | None = None) -> int:
    """BallDontLie labels a season by the year it starts (e.g. 2025 for the 2025-26 season,
    which runs roughly Oct 2025 - Jun 2026)."""
    now = now or datetime.now(UTC)
    return now.year if now.month >= 10 else now.year - 1


def _map_status(status: str, period: int | None) -> str:
    """BallDontLie's `status` has no fixed enum: "Final" once done, a start-time string like
    "7:00 pm ET" before tip-off, or a period string ("1st Qtr", "Halftime", ...) while live.
    Mapped heuristically to our fixtures.status values."""
    if status == "Final":
        return "completed"
    if period:
        return "live"
    return "scheduled"


def _map_game_to_fixture_payload(game: dict) -> FixturePayload:
    """Pure, network/DB-free mapping — kept separate so it's directly unit-testable against a
    recorded sample response."""
    home = game["home_team"]
    away = game["visitor_team"]
    return FixturePayload(
        external_id=str(game["id"]),
        league_external_id="nba",
        home_team_external_id=str(home["id"]),
        away_team_external_id=str(away["id"]),
        kickoff_utc=datetime.fromisoformat(game["datetime"].replace("Z", "+00:00")),
        season=str(game["season"]),
        home_team_name=home.get("full_name") or home.get("name"),
        away_team_name=away.get("full_name") or away.get("name"),
        home_team_short_name=home.get("abbreviation"),
        away_team_short_name=away.get("abbreviation"),
        status=_map_status(game.get("status", ""), game.get("period")),
        home_score=game.get("home_team_score"),
        away_score=game.get("visitor_team_score"),
        # BallDontLie's "period" is a quarter number (1-4, OT beyond), not a clock minute —
        # a different unit from match_minute, so left None rather than mismapped.
    )


def _compute_team_stats(team_external_id: str, games: list[dict], n_matches: int) -> TeamStats:
    """Derives what it can from the basic /games endpoint (TDD §3.3's own NBA feature table
    sources last-10 win rate, point differential, and rest days from this same endpoint).
    elo_rating/xg_for_5/xg_against_5 stay None — no equivalent is available without the
    gated advanced-stats/season-averages endpoints (401 on the current key's plan)."""

    def is_home(g: dict) -> bool:
        return str(g["home_team"]["id"]) == team_external_id

    def team_score(g: dict) -> int:
        return g["home_team_score"] if is_home(g) else g["visitor_team_score"]

    def opp_score(g: dict) -> int:
        return g["visitor_team_score"] if is_home(g) else g["home_team_score"]

    def won(g: dict) -> bool:
        return team_score(g) > opp_score(g)

    completed = [g for g in games if g.get("status") == "Final"]
    completed.sort(key=lambda g: g["datetime"], reverse=True)

    recent = completed[:n_matches]
    # NBA has no draws, so this is a plain 0.0-1.0 win-rate fraction over the recent window —
    # not the football-style 3/1/0 points-per-game convention the field name suggests.
    form_pts_5 = (sum(1 for g in recent if won(g)) / len(recent)) if recent else None
    attack_str = (sum(team_score(g) for g in recent) / len(recent)) if recent else None
    defence_str = (sum(opp_score(g) for g in recent) / len(recent)) if recent else None

    home_games = [g for g in completed if is_home(g)]
    away_games = [g for g in completed if not is_home(g)]
    home_win_rate = (sum(1 for g in home_games if won(g)) / len(home_games)) if home_games else None
    away_win_rate = (sum(1 for g in away_games if won(g)) / len(away_games)) if away_games else None

    # Season-long (all `completed`, not just `recent`) average point differential — a "net
    # rating" proxy for app/models_ml/nba_features.py, distinct from the last-N form signal.
    season_point_diff = (
        (sum(team_score(g) - opp_score(g) for g in completed) / len(completed))
        if completed
        else None
    )

    days_since_last_match = None
    if completed:
        last_date = datetime.fromisoformat(completed[0]["datetime"].replace("Z", "+00:00"))
        days_since_last_match = (datetime.now(UTC) - last_date).days

    return TeamStats(
        team_external_id=team_external_id,
        elo_rating=None,
        attack_str=attack_str,
        defence_str=defence_str,
        form_pts_5=form_pts_5,
        xg_for_5=None,
        xg_against_5=None,
        days_since_last_match=days_since_last_match,
        home_win_rate=home_win_rate,
        away_win_rate=away_win_rate,
        season_point_diff=season_point_diff,
    )


MAX_RETRIES = 5  # on 429 specifically — the free tier's per-minute limit is tight and easy
# to hit on a batch of team-stats lookups (confirmed live: ~4-5 calls in quick succession).


async def _get_with_retry(client: httpx.AsyncClient, path: str, params: dict) -> httpx.Response:
    """Retries on 429, honouring Retry-After when present, else capped exponential backoff.
    Any other error status raises immediately — this is not a general-purpose retry-on-
    anything helper."""
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
    response.raise_for_status()  # exhausted retries — surface the last 429 as a real error
    return response


async def _fetch_all_games(client: httpx.AsyncClient, params: dict) -> list[dict]:
    """Follows BallDontLie's cursor pagination (meta.next_cursor), not offset-based."""
    games: list[dict] = []
    cursor = None
    for _ in range(MAX_PAGES):
        query = dict(params)
        if cursor is not None:
            query["cursor"] = cursor
        response = await _get_with_retry(client, "/games", query)
        payload = response.json()
        games.extend(payload.get("data", []))
        cursor = payload.get("meta", {}).get("next_cursor")
        if not cursor:
            break
    return games


class BallDontLieAdapter(DataSourceAdapter):
    """NBA live production stats: games (real, implemented below) plus advanced stats/season
    averages/standings/injuries (TDD §2.1/§2.2/§7) — this is the sole live NBA stats source;
    nba_api/stats.nba.com is offline-training-only and must never be called from a production
    worker (it blocks cloud-provider IPs). fetch_odds/fetch_injuries remain unimplemented —
    out of scope for the current task."""

    def __init__(self) -> None:
        self._api_key = get_settings().balldontlie_api_key

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=BASE_URL, headers={"Authorization": self._api_key}, timeout=10.0
        )

    async def fetch_odds(self, sport: str, league: str, days_ahead: int) -> list[OddsPayload]:
        raise NotImplementedError("BallDontLie does not provide odds — use TheRundownAdapter")

    async def fetch_fixtures(
        self, sport: str, league: str, days_ahead: int, days_back: int = 0
    ) -> list[FixturePayload]:
        now = datetime.now(UTC)
        params = {
            "start_date": (now - timedelta(days=days_back)).date().isoformat(),
            "end_date": (now + timedelta(days=days_ahead)).date().isoformat(),
            "per_page": 100,
        }
        async with self._client() as client:
            games = await _fetch_all_games(client, params)
        return [_map_game_to_fixture_payload(g) for g in games]

    async def fetch_team_stats(
        self, team_id: str, n_matches: int, league: str | None = None
    ) -> TeamStats:
        # league is ignored — NBA has exactly one league, and _current_nba_season() already
        # derives the season internally from the current date (see its own docstring).
        params = {"team_ids[]": team_id, "seasons[]": _current_nba_season(), "per_page": 100}
        async with self._client() as client:
            games = await _fetch_all_games(client, params)
        return _compute_team_stats(team_id, games, n_matches)

    async def fetch_injuries(self, sport: str) -> list[InjuryUpdate]:
        """Fallback path when ROTOWIRE_API_KEY is absent (TDD §2.3). Less real-time than
        RotoWire — no GTD → OUT transition alerts — and the re-inference trigger stays
        disabled when this path is used."""
        raise NotImplementedError("BallDontLie injury fetch not yet implemented")


H2H_SEASONS_BACK = 3  # how many recent seasons to search for head-to-head meetings


async def fetch_h2h_win_rate(home_external_id: str, away_external_id: str) -> float | None:
    """NBA/BallDontLie-specific helper, not part of the DataSourceAdapter ABC — H2H is
    fixture-specific (needs both teams), not a generic per-team stat, so it doesn't fit
    fetch_team_stats(team_id)'s shape. Used by
    app/models_ml/nba_features.py:assemble_from_live_db. Searches the home team's own game
    history for meetings against the away team rather than a dedicated endpoint (BallDontLie
    has none) — each game a team's history includes already embeds both participants."""
    api_key = get_settings().balldontlie_api_key
    current_season = _current_nba_season()
    seasons = [current_season - i for i in range(H2H_SEASONS_BACK)]

    async with httpx.AsyncClient(
        base_url=BASE_URL, headers={"Authorization": api_key}, timeout=10.0
    ) as client:
        games = await _fetch_all_games(
            client, {"team_ids[]": home_external_id, "seasons[]": seasons, "per_page": 100}
        )

    def is_home(g: dict) -> bool:
        return str(g["home_team"]["id"]) == home_external_id

    def opponent_id(g: dict) -> str:
        return str(g["visitor_team"]["id"]) if is_home(g) else str(g["home_team"]["id"])

    def home_team_won(g: dict) -> bool:
        if is_home(g):
            return g["home_team_score"] > g["visitor_team_score"]
        return g["visitor_team_score"] > g["home_team_score"]

    meetings = [
        g for g in games if g.get("status") == "Final" and opponent_id(g) == away_external_id
    ]
    if not meetings:
        return None
    return sum(1 for g in meetings if home_team_won(g)) / len(meetings)
