"""ATP + WTA tennis, both served by BallDontLie (same account/key as the existing NBA
adapter) but under two entirely separate API namespaces (/atp/v1/*, /wta/v1/*) rather than
one endpoint filtered by a tour param — see the real OpenAPI specs at
https://www.balldontlie.io/openapi/atp.yml and .../wta.yml (fetched during planning, not
guessed). One adapter class covers both; the tour is selected via the `league` parameter
DataSourceAdapter's fetch_fixtures/fetch_team_stats already carry (league="atp"|"wta").

TIER GATING (confirmed live via the real OpenAPI specs): /players, /tournaments, /rankings
are on the Free plan. /matches, /matches/{id} require ALL-STAR. /match_stats,
/player_career_stats, /head_to_head, /odds, /odds/opening require GOAT. This adapter's core
methods (fetch_fixtures, fetch_team_stats, the standalone H2H/surface helpers) all depend on
/matches, so NONE of them are live-callable below ALL-STAR tier. The pure mapping functions
below are unit-tested against recorded-shape JSON (no network) and are believed correct per
the spec, but have NOT been live-verified against a real response — see CLAUDE.md for the
current tier status. Treat exact field names here (especially kickoff-time and nested
player/tournament shapes) as best-effort until a real call confirms them, the same
"correct code, no live credential yet" status this codebase already has for RotoWire/
BallDontLie NBA injuries and EAS push tokens.

TOUR-PREFIXED EXTERNAL IDS: ATP and WTA are one shared sport_id="tennis" (see
scripts/seed_sports.py:seed_tennis), but app/fixtures/service.py:get_or_create_team and
Fixture's own unique constraint (uq_fixtures_sport_external_id) key only on
(sport_id, external_id), not league_id. Since BallDontLie's two tour namespaces have
independently-sequenced integer IDs, an unprefixed id=142 in both tours would silently
collide into one Team/Fixture row. Every external_id this adapter produces is therefore
prefixed f"{tour}:{raw_id}" — never the raw provider id alone.

A tennis "team" is a single player — Team/TeamStats/TeamFeatures have no roster/multi-player
assumption anywhere in this codebase, so no schema change was needed for that.

Outcome.home_score/away_score (non-nullable ints, app/history/models.py) = SETS WON (e.g.
2-0, 3-1), not games or points — this is what _maybe_settle_outcome's win/loss/draw
derivation and the free, already-wired Team.elo_rating auto-update
(app/models_ml/elo.py:apply_match_result, which only reads the sign of the difference) both
key off. Tennis has no draws, so MatchResult.DRAW never fires here, matching NBA.

Status mapping is simpler than football's: "finished"/"walkover"/"retired"/"defaulted" all
map to "completed" (a real result exists in every case — walkover/retired/defaulted are just
early/no-play endings, not a game that never happened), so no FixtureStatus.POSTPONED
mapping is needed here. NOTE: football's own _map_status buckets its "WO" (walkover/awarded)
status into POSTPONED instead, on the reasoning that a forfeited game has no real market to
show. Whether tennis's zero-play "walkover" should follow that same precedent (rather than
completed with a synthetic 0-0-shaped score) is a real, live-data-dependent judgment call —
revisit once real match_status proportions are visible (ALL-STAR tier)."""

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

BASE_URL = "https://api.balldontlie.io"
_TOUR_PREFIXES = {"atp": "/atp/v1", "wta": "/wta/v1"}
MAX_PAGES = 20  # safety cap on cursor-pagination loops, mirrors balldontlie.py's NBA adapter

# Every match_status value that means a real result exists (see module docstring for the
# walkover/retired/defaulted judgment call).
_COMPLETED_MATCH_STATUSES = {"finished", "walkover", "retired", "defaulted"}

MAX_RETRIES = 5  # on 429 — same rationale as balldontlie.py's NBA adapter


def _tour_prefix(league: str) -> str:
    prefix = _TOUR_PREFIXES.get(league)
    if prefix is None:
        raise ValueError(f"Unknown tennis tour/league={league!r} (expected 'atp' or 'wta')")
    return prefix


def _external_id(tour: str, raw_id) -> str:
    return f"{tour}:{raw_id}"


def _strip_tour_prefix(external_id: str) -> str:
    """external_id here is always OUR tour-prefixed id (f"{tour}:{raw_id}") — strips it back
    to the raw provider id needed for an actual API call."""
    return external_id.split(":", 1)[1] if ":" in external_id else external_id


def _raw_player_id(player: dict | None) -> str | None:
    if not player:
        return None
    return str(player.get("id")) if player.get("id") is not None else None


def _map_status(match_status: str) -> str:
    if match_status in _COMPLETED_MATCH_STATUSES:
        return "completed"
    if match_status == "in_progress":
        return "live"
    return "scheduled"


def _match_date(match: dict) -> date:
    """No per-match kickoff-time field was confirmed in the real OpenAPI spec extraction
    (see module docstring) — try a few plausible field names first, then fall back to the
    tournament's own start_date. Returns a date (not datetime) since that's all a tournament
    start_date can offer as a fallback anyway."""
    raw = match.get("scheduled_at") or match.get("start_time") or match.get("date")
    if raw:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    return date.fromisoformat(str(match["tournament"]["start_date"])[:10])


def _match_kickoff_utc(match: dict) -> datetime:
    raw = match.get("scheduled_at") or match.get("start_time") or match.get("date")
    if raw:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    tournament_start = str(match["tournament"]["start_date"])[:10]
    return datetime.fromisoformat(tournament_start).replace(tzinfo=UTC)


def _sets_won(match: dict) -> tuple[int | None, int | None]:
    """(player1 sets won, player2 sets won) from set_scores — the real "score" convention
    Outcome.home_score/away_score use for tennis (see module docstring). None, None when
    set_scores is genuinely absent (e.g. a scheduled/live match, or a walkover with no sets
    played at all) rather than fabricating 0-0."""
    set_scores = match.get("set_scores") or []
    if not set_scores:
        return None, None
    p1_sets = sum(
        1 for s in set_scores if (s.get("player1_games") or 0) > (s.get("player2_games") or 0)
    )
    p2_sets = sum(
        1 for s in set_scores if (s.get("player2_games") or 0) > (s.get("player1_games") or 0)
    )
    return p1_sets, p2_sets


def _match_winner_id(match: dict) -> str | None:
    return _raw_player_id(match.get("winner"))


def _match_completed(match: dict) -> bool:
    return match.get("match_status") in _COMPLETED_MATCH_STATUSES


def _opponent(match: dict, player_external_id: str) -> dict | None:
    if _raw_player_id(match.get("player1")) == player_external_id:
        return match.get("player2")
    return match.get("player1")


def _map_match_to_fixture_payload(match: dict, tour: str) -> FixturePayload:
    """Pure, network/DB-free mapping — mirrors every other adapter's _map_*_to_fixture_payload
    (e.g. balldontlie.py's NBA version), directly unit-testable against a recorded shape."""
    player1 = match["player1"]
    player2 = match["player2"]
    home_sets, away_sets = _sets_won(match)
    status = _map_status(match.get("match_status", ""))

    return FixturePayload(
        external_id=_external_id(tour, match["id"]),
        league_external_id=tour,
        home_team_external_id=_external_id(tour, player1["id"]),
        away_team_external_id=_external_id(tour, player2["id"]),
        kickoff_utc=_match_kickoff_utc(match),
        season=str(match.get("season") or match["tournament"]["season"]),
        home_team_name=player1.get("full_name"),
        away_team_name=player2.get("full_name"),
        status=status,
        home_score=home_sets if status == "completed" else None,
        away_score=away_sets if status == "completed" else None,
    )


def _current_streak(
    completed_sorted_desc: list[dict], player_external_id: str
) -> tuple[float, float]:
    """Consecutive win/loss streak walking backward from the most recent match — exactly one
    of the two is ever positive, mirroring api_football.py:_parse_streaks' own convention."""
    win_streak = 0
    losing_streak = 0
    for m in completed_sorted_desc:
        won = _match_winner_id(m) == player_external_id
        if win_streak == 0 and losing_streak == 0:
            if won:
                win_streak = 1
            else:
                losing_streak = 1
        elif win_streak > 0:
            if won:
                win_streak += 1
            else:
                break
        else:
            if not won:
                losing_streak += 1
            else:
                break
    return float(win_streak), float(losing_streak)


def _latest_rank_points(rankings: list[dict]) -> float | None:
    if not rankings:
        return None
    latest = max(rankings, key=lambda r: r.get("ranking_date") or "")
    points = latest.get("points")
    return float(points) if points is not None else None


def _compute_team_stats(
    player_external_id: str, matches: list[dict], rankings: list[dict], n_matches: int
) -> TeamStats:
    """No real "home/away" (neutral-site sport) or goals-scoring concept exists for tennis —
    attack_str/defence_str/home_win_rate/away_win_rate/xg_for_5/xg_against_5/season_point_diff
    all stay None (never fabricated), matching this codebase's "never fabricate a neutral
    value" convention. elo_rating also stays None here: Team.elo_rating (the real, persistent
    value) is populated separately and generically by
    app/workers/ingest_fixtures.py:_maybe_settle_outcome for every sport, not by this
    adapter's own TeamStats — see module docstring."""
    completed = [m for m in matches if _match_completed(m)]
    completed.sort(key=_match_date, reverse=True)
    recent = completed[:n_matches]

    form_win_rate = (
        (sum(1 for m in recent if _match_winner_id(m) == player_external_id) / len(recent))
        if recent
        else None
    )
    win_streak, losing_streak = (
        _current_streak(completed, player_external_id) if completed else (None, None)
    )
    days_since_last_match = (
        (datetime.now(UTC).date() - _match_date(completed[0])).days if completed else None
    )

    return TeamStats(
        team_external_id=player_external_id,
        form_pts_5=form_win_rate,
        days_since_last_match=days_since_last_match,
        win_streak=win_streak,
        losing_streak=losing_streak,
        rank_points=_latest_rank_points(rankings),
    )


async def _get_with_retry(client: httpx.AsyncClient, path: str, params: dict) -> httpx.Response:
    """Retries on 429, honouring Retry-After when present, else capped exponential backoff —
    copied from balldontlie.py's NBA adapter rather than factored into a shared module: no
    shared HTTP-helper module exists in this codebase today (api_football.py has no retry
    logic at all), so factoring now would be premature abstraction against actual precedent."""
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


async def _fetch_all_pages(client: httpx.AsyncClient, path: str, params: dict) -> list[dict]:
    """Cursor pagination (meta.next_cursor) — same convention as BallDontLie's NBA API."""
    results: list[dict] = []
    cursor = None
    for _ in range(MAX_PAGES):
        query = dict(params)
        if cursor is not None:
            query["cursor"] = cursor
        response = await _get_with_retry(client, path, query)
        payload = response.json()
        results.extend(payload.get("data", []))
        cursor = payload.get("meta", {}).get("next_cursor")
        if not cursor:
            break
    return results


def _tournament_overlaps_window(tournament: dict, window_start: date, window_end: date) -> bool:
    t_start = date.fromisoformat(str(tournament["start_date"])[:10])
    t_end = date.fromisoformat(str(tournament["end_date"])[:10])
    return t_start <= window_end and t_end >= window_start


class BallDontLieTennisAdapter(DataSourceAdapter):
    """ATP + WTA. fetch_fixtures deliberately does NOT assume /matches supports a date-range
    filter (unconfirmed in the real spec, and tennis matches are tournament-scoped, unlike
    NBA's /games) — instead it lists tournaments overlapping the window via /tournaments
    (Free tier, has real start_date/end_date fields) and fetches /matches per tournament_id."""

    def __init__(self) -> None:
        self._api_key = get_settings().balldontlie_api_key

    def _client(self, tour_prefix: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=BASE_URL + tour_prefix,
            headers={"Authorization": self._api_key},
            timeout=10.0,
        )

    async def fetch_odds(self, sport: str, league: str, days_ahead: int) -> list[OddsPayload]:
        raise NotImplementedError(
            "BallDontLie tennis odds require the GOAT tier — not wired up yet (predictions "
            "ship first, odds are an explicit fast-follow; see CLAUDE.md)"
        )

    async def fetch_fixtures(
        self, sport: str, league: str, days_ahead: int, days_back: int = 0
    ) -> list[FixturePayload]:
        tour = league
        prefix = _tour_prefix(tour)
        now = datetime.now(UTC)
        window_start = (now - timedelta(days=days_back)).date()
        window_end = (now + timedelta(days=days_ahead)).date()

        async with self._client(prefix) as client:
            tournaments = await _fetch_all_pages(client, "/tournaments", {"per_page": 100})
            overlapping = [
                t for t in tournaments if _tournament_overlaps_window(t, window_start, window_end)
            ]
            payloads: list[FixturePayload] = []
            for tournament in overlapping:
                matches = await _fetch_all_pages(
                    client, "/matches", {"tournament_id": tournament["id"], "per_page": 100}
                )
                for match in matches:
                    match.setdefault("tournament", tournament)
                    payloads.append(_map_match_to_fixture_payload(match, tour))
        return payloads

    async def fetch_team_stats(
        self, team_id: str, n_matches: int, league: str | None = None
    ) -> TeamStats:
        if league is None:
            raise ValueError(
                "BallDontLieTennisAdapter.fetch_team_stats requires league='atp'|'wta'"
            )
        prefix = _tour_prefix(league)
        raw_player_id = _strip_tour_prefix(team_id)
        async with self._client(prefix) as client:
            matches = await _fetch_all_pages(
                client, "/matches", {"player_ids[]": raw_player_id, "per_page": 100}
            )
            rankings = await _fetch_all_pages(
                client, "/rankings", {"player_ids[]": raw_player_id, "per_page": 100}
            )
        return _compute_team_stats(raw_player_id, matches, rankings, n_matches)

    async def fetch_injuries(self, sport: str) -> list[InjuryUpdate]:
        """No tennis injury feed at MVP — same as every non-NBA/football sport today."""
        return []


async def fetch_h2h_win_rate_tennis(
    tour: str, player_external_id: str, opponent_external_id: str
) -> float | None:
    """Standalone, not part of the DataSourceAdapter ABC — mirrors
    balldontlie.py:fetch_h2h_win_rate's existing NBA precedent (fixture-specific, doesn't fit
    fetch_team_stats(team_id)'s per-team shape). Derived manually from the player's own match
    history rather than the GOAT-gated /head_to_head endpoint, so this stays reachable at
    ALL-STAR tier alone. external_id args are OUR tour-prefixed ids; stripped internally."""
    api_key = get_settings().balldontlie_api_key
    prefix = _tour_prefix(tour)
    raw_player_id = _strip_tour_prefix(player_external_id)
    raw_opponent_id = _strip_tour_prefix(opponent_external_id)

    async with httpx.AsyncClient(
        base_url=BASE_URL + prefix, headers={"Authorization": api_key}, timeout=10.0
    ) as client:
        matches = await _fetch_all_pages(
            client, "/matches", {"player_ids[]": raw_player_id, "per_page": 100}
        )

    meetings = [
        m
        for m in matches
        if _match_completed(m) and _raw_player_id(_opponent(m, raw_player_id)) == raw_opponent_id
    ]
    if not meetings:
        return None
    return sum(1 for m in meetings if _match_winner_id(m) == raw_player_id) / len(meetings)


async def fetch_surface_win_rate(
    tour: str, player_external_id: str, match_external_id: str
) -> float | None:
    """Standalone, not part of the ABC — surface win rate is fixture-specific (the CURRENT
    tournament's surface), which doesn't fit fetch_team_stats' per-team-per-ingest-run cache
    (see app/workers/ingest_fixtures.py's team_stats_cache). Deliberately does one extra live
    call to /matches/{id} to read the current match's own tournament.surface rather than
    require a new persisted column — mirrors how NBA's h2h_win_rate_home is a live call, not
    a cached TeamFeatures column. external_id args are OUR tour-prefixed ids; stripped
    internally."""
    api_key = get_settings().balldontlie_api_key
    prefix = _tour_prefix(tour)
    raw_player_id = _strip_tour_prefix(player_external_id)
    raw_match_id = _strip_tour_prefix(match_external_id)

    async with httpx.AsyncClient(
        base_url=BASE_URL + prefix, headers={"Authorization": api_key}, timeout=10.0
    ) as client:
        match_response = await _get_with_retry(client, f"/matches/{raw_match_id}", {})
        current_match = match_response.json().get("data", {})
        surface = current_match.get("tournament", {}).get("surface")
        if not surface:
            return None

        matches = await _fetch_all_pages(
            client, "/matches", {"player_ids[]": raw_player_id, "per_page": 100}
        )

    same_surface = [
        m
        for m in matches
        if _match_completed(m) and m.get("tournament", {}).get("surface") == surface
    ]
    if not same_surface:
        return None
    return sum(1 for m in same_surface if _match_winner_id(m) == raw_player_id) / len(same_surface)
