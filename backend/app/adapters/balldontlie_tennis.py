"""ATP + WTA tennis, both served by BallDontLie (same account/key as the existing NBA
adapter) but under two entirely separate API namespaces (/atp/v1/*, /wta/v1/*) rather than
one endpoint filtered by a tour param — see the real OpenAPI specs at
https://www.balldontlie.io/openapi/atp.yml and .../wta.yml (fetched during planning, not
guessed). One adapter class covers both; the tour is selected via the `league` parameter
DataSourceAdapter's fetch_fixtures/fetch_team_stats already carry (league="atp"|"wta").

TIER GATING (confirmed live via the real OpenAPI specs): /players, /tournaments, /rankings
are on the Free plan. /matches, /matches/{id} require ALL-STAR. /match_stats,
/player_career_stats, /head_to_head, /odds, /odds/opening require GOAT. The user's real
BallDontLie subscription (confirmed live) is ALL-STAR for ATP only — WTA still 401s on
/matches until that tour is separately subscribed (a real, live-confirmed 401, isolated by
the per-league exception handling in ingest_fixtures.py/ingest_live_scores.py so it can never
block ATP or any other sport). Every field name/shape below (scheduled_time, tournament
nesting, set_scores, rankings' date param, season-scoped /matches) is now real-response
confirmed against live ATP data, not guessed from the OpenAPI spec.

REAL, LIVE-CONFIRMED API BEHAVIOR (not in the OpenAPI spec extraction):
  - `scheduled_time` is the per-match kickoff field, but it is USUALLY ABSENT. An earlier
    version of this note called it "the real per-match kickoff field" on the strength of a
    handful of sampled matches; measured across a full real ATP tournament (600 matches) that
    is wrong: 570 had no `scheduled_time` at all, and of the 30 that did, 17 were exactly
    midnight (a date, not a time). Only ~2% carry a genuine kickoff time. Callers must use
    _match_kickoff_is_estimated alongside _match_kickoff_utc and must not present an estimated
    kickoff as a real one — see that function for why this mattered in production.
  - `/matches?player_ids[]=X` with NO `season` param returns only a thin, recent window
    (confirmed live: ~3.5 months for a real, active player) — NOT full career history. Full
    history requires looping an explicit `season` param per year (confirmed working back to
    at least 2021). Every "recent history" computation below (form/streak/H2H/surface stats)
    therefore explicitly loops TENNIS_SEASONS_BACK seasons rather than relying on the
    unfiltered default.
  - `/rankings?player_ids[]=X` with NO `date` param returns only the CURRENT ranking (a single
    row) — real historical point-in-time rankings require an explicit `date=YYYY-MM-DD` param
    (confirmed live: this resolves to that player's real ranking as of the most recent
    snapshot on/before that date, e.g. `date=2025-06-01` correctly returned the
    `ranking_date=2025-05-26` snapshot). `/rankings?season=X` with no player filter does NOT
    give a historical sweep — it silently ignores `season` and returns the current top-500
    regardless. This is the mechanism ml/training/collect_tennis_data.py uses for leakage-safe
    historical rank_diff (see that script's own docstring for the per-player-per-week caching
    this implies).

HOME/AWAY IS NOT match["player1"]/["player2"] — a real, serious bug found and fixed after the
first training run: BallDontLie always lists the eventual WINNER as player1 for a completed
match (confirmed live: 20/20 in a sampled batch of real completed 2022 matches). Naively
treating player1="home" bakes 100% target leakage into every completed match's outcome label
— this is exactly what corrupted ml/training/collect_tennis_data.py's first real training run
(train/val seasons came back 100% "home won", since every one of those matches was already
completed and therefore already player1-reordered). A genuinely SCHEDULED match (winner
unknown) does NOT show this pattern — confirmed live against real upcoming 2026 matches — so
this is specifically a completed-match reordering, not a fixed pre-game listing convention.
_home_away_players() fixes this with a stable, outcome-independent tiebreak (lower external
player id = home) applied identically regardless of match status, so a fixture's home/away
identity can never flip between an early (scheduled) ingest and a later (completed) one, and
so training labels are the honest, no-leakage signal tennis actually has no real
home-advantage semantics to leak in the first place (see tennis_features.py's deliberate
home_court_indicator omission — "home" here is purely a label-stability device, not a real
signal).

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
    H2HPanel,
    H2HPanelStat,
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

# How many seasons back to loop for "recent history" computations (form/streak/H2H/surface
# stats) — confirmed live that an unfiltered /matches?player_ids[]=X call only returns a thin
# ~3.5-month window, not full history, so every function below explicitly loops seasons
# instead. Mirrors balldontlie.py's own H2H_SEASONS_BACK=3 precedent for NBA.
TENNIS_SEASONS_BACK = 3


def _current_tennis_season(now: datetime | None = None) -> int:
    """ATP/WTA seasons are calendar-year (confirmed live: /tournaments?season=2025 returns
    real Jan-Dec 2025 tournaments) — unlike football's Aug-May convention, no month-boundary
    logic is needed."""
    now = now or datetime.now(UTC)
    return now.year


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
    """`scheduled_time` is the real, live-confirmed per-match kickoff field (confirmed against
    a real /atp/v1/matches response — earlier guesses at scheduled_at/start_time/date were
    wrong). Falls back to the tournament's own start_date only for the rare case a match
    genuinely has no scheduled_time (returns a date, not datetime, since that's all a
    tournament start_date can offer)."""
    raw = match.get("scheduled_time")
    if raw:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    return date.fromisoformat(str(match["tournament"]["start_date"])[:10])


def _is_same_edition(match: dict, tournament: dict) -> bool:
    """Whether this match belongs to the CURRENT running of `tournament`, not a past year's.

    `/matches?tournament_ids[]=X` returns every match ever played under that id, across every
    edition — a tournament id identifies the event, not the year's running of it. Measured
    live: the National Bank Open id returns 1,452 matches spanning 19 editions back to 2007, of
    which only 70 are the current one; the Washington id returns 1,164 for 49 current. Left
    unfiltered a routine +/-2 day poll ingests 2,616 fixtures instead of 119, which is how
    several thousand fixtures dating to 2007 ended up in the database. An earlier note recorded
    those as leftover test data and confirmed the tournament-window logic was sound — that
    check was on TOURNAMENT scoping, which is correct; the per-match scoping underneath it was
    never checked, and is where the leak is.

    The discriminator is the edition's own start_date, which each match carries on its embedded
    tournament object. Filtering on the MATCH date instead would look simpler but breaks:
    96% of matches carry no scheduled_time at all and fall back to the tournament start, so
    once the poll window moved past a tournament's opening day, every remaining match in it —
    including ones being played right now — would be filtered out.
    """
    embedded = match.get("tournament") or {}
    theirs = str(embedded.get("start_date") or "")[:10]
    ours = str(tournament.get("start_date") or "")[:10]
    if not theirs or not ours:
        # No basis to judge — keep it. Over-ingesting one match is recoverable; silently
        # dropping a real upcoming fixture is not.
        return True
    return theirs == ours


def _match_kickoff_is_estimated(match: dict) -> bool:
    """Whether this match's kickoff had to be INFERRED rather than read from the provider.

    Measured across a full real ATP tournament (600 matches): 570 had no `scheduled_time` at
    all, and of the 30 that did, 17 were exactly midnight — a date with no time of day. Only
    ~2% carried a genuine kickoff time. An earlier note in CLAUDE.md called `scheduled_time`
    "the real per-match kickoff field" on the strength of a small sample; at scale that is
    wrong, and this function exists to stop the codebase asserting a precision it does not have.

    A midnight timestamp is treated as estimated for the same reason a missing one is: real
    tennis matches are not scheduled for 00:00 UTC, so it encodes a date, not a time."""
    raw = match.get("scheduled_time")
    if not raw:
        return True
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return (parsed.hour, parsed.minute, parsed.second) == (0, 0, 0)


def _match_kickoff_utc(match: dict) -> datetime:
    """Best available kickoff. Callers must pair this with _match_kickoff_is_estimated — when
    that returns True this is a DATE placeholder (the provider's own midnight timestamp, or the
    tournament's start date), not a real start time, and must not be displayed as one."""
    raw = match.get("scheduled_time")
    if raw:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    tournament_start = str(match["tournament"]["start_date"])[:10]
    return datetime.fromisoformat(tournament_start).replace(tzinfo=UTC)


def _is_completed_set(p1_games: int, p2_games: int) -> bool:
    """Whether a set was actually WON, per real tennis scoring — not merely "someone is ahead".

    A set needs 6+ games with a 2-game margin (6-0..6-4, 7-5, and long deciding sets like
    70-68), or a 7-6 tiebreak. A partial score like 2-3 (play abandoned mid-set when a player
    retires) is NOT a won set, and neither is 6-5 (play continues to 7-5 or 7-6).

    This is the fix for a real, user-reported bug: the previous implementation counted ANY set
    where a player happened to lead, so a real retirement (Popyrin 6-4, 2-3 vs Kokkinakis, who
    retired) was scored 1-1 — an impossible tennis scoreline, since tennis has no draws. Worse,
    it inverted the win/loss verdict: Popyrin genuinely WON, but a stored 1-1 made the mobile
    feed mark a CORRECT prediction as a failure, and made _maybe_settle_outcome derive a
    MatchResult.DRAW that can never happen in tennis."""
    hi, lo = max(p1_games, p2_games), min(p1_games, p2_games)
    if hi == 7 and lo == 6:
        return True  # tiebreak
    return hi >= 6 and (hi - lo) >= 2


def _sets_won(match: dict) -> tuple[int | None, int | None]:
    """(player1 sets won, player2 sets won) from set_scores, counting only genuinely COMPLETED
    sets (see _is_completed_set) — the real "score" convention Outcome.home_score/away_score
    use for tennis (see module docstring). None, None when set_scores is genuinely absent (e.g.
    a scheduled/live match, or a walkover with no sets played at all) rather than fabricating
    0-0."""
    set_scores = match.get("set_scores") or []
    if not set_scores:
        return None, None
    p1_sets = 0
    p2_sets = 0
    for s in set_scores:
        p1_games, p2_games = (s.get("player1_games") or 0), (s.get("player2_games") or 0)
        if not _is_completed_set(p1_games, p2_games):
            continue  # abandoned/in-progress set — belongs to neither player
        if p1_games > p2_games:
            p1_sets += 1
        else:
            p2_sets += 1
    return p1_sets, p2_sets


# Every real tennis format (best-of-3, best-of-5) needs at least 2 won sets to take the match,
# so a "winner" with fewer than this did not win on court — the match ended irregularly.
MIN_SETS_TO_WIN_A_MATCH = 2


def _match_result_type(match: dict) -> str | None:
    """None for a normally-completed match; "retired" / "walkover" for one that ended without
    being played out. Detected STRUCTURALLY, not from match_status — a real, live-confirmed
    finding that invalidated this adapter's original assumption: BallDontLie reported a genuine
    mid-match retirement (Popyrin/Kokkinakis, real set_scores 6-4, 2-3) as plain
    match_status="finished", with no retirement marker anywhere in the response. Relying on
    _COMPLETED_MATCH_STATUSES' "retired"/"walkover"/"defaulted" values alone would therefore
    silently miss real retirements, so this instead infers it from the score itself:

      - a set left incomplete (play abandoned mid-set), or
      - a winner who never reached MIN_SETS_TO_WIN_A_MATCH won sets

    Either means the result stands (there IS a real winner) but the match wasn't played out —
    which is why these render as a neutral "RET" badge with NO win/loss verdict rather than
    counting against the model: most bookmakers void bets on a retirement, so showing a tick
    would imply a payout the user may never have received. The provider's own explicit
    match_status is still honoured when present, since a "walkover" with zero sets played can
    only be identified that way."""
    status = (match.get("match_status") or "").lower()
    if status in ("walkover", "defaulted"):
        return "walkover"
    if status == "retired":
        return "retired"

    if match.get("winner") is None:
        return None  # not finished — nothing to classify yet

    set_scores = match.get("set_scores") or []
    if not set_scores:
        # A finished match with a real winner but no sets at all was never played.
        return "walkover"

    if any(
        not _is_completed_set((s.get("player1_games") or 0), (s.get("player2_games") or 0))
        for s in set_scores
    ):
        return "retired"

    p1_sets, p2_sets = _sets_won(match)
    if max(p1_sets or 0, p2_sets or 0) < MIN_SETS_TO_WIN_A_MATCH:
        return "retired"
    return None


def _match_winner_id(match: dict) -> str | None:
    return _raw_player_id(match.get("winner"))


def _home_away_players(match: dict) -> tuple[dict, dict]:
    """(home_player, away_player) via a stable, outcome-independent tiebreak — NOT
    match["player1"]/["player2"] directly. Live-confirmed (a sampled batch of completed ATP
    matches, 20/20): BallDontLie always lists the eventual WINNER as player1 for a completed
    match — that ordering is a disguised outcome variable, not a neutral position, and using
    it as "home" would bake 100% target leakage into every completed match's label (this is
    exactly what corrupted the first training run — see ml/training/collect_tennis_data.py's
    module docstring for the full story). Tennis has no real home-court concept anyway (see
    tennis_features.py's deliberate home_court_indicator omission), so "home" here is just a
    fixed tiebreak (lower external player id) applied identically whether the match is still
    scheduled or already completed — this also keeps a fixture's home/away identity stable
    across ingest runs as its status transitions, since player1/player2 may get reordered by
    the provider once a match settles but the id-based rule never does."""
    player1, player2 = match["player1"], match["player2"]
    if int(player1["id"]) < int(player2["id"]):
        return player1, player2
    return player2, player1


def _match_completed(match: dict) -> bool:
    return match.get("match_status") in _COMPLETED_MATCH_STATUSES


def _opponent(match: dict, player_external_id: str) -> dict | None:
    if _raw_player_id(match.get("player1")) == player_external_id:
        return match.get("player2")
    return match.get("player1")


def _map_match_to_fixture_payload(match: dict, tour: str) -> FixturePayload:
    """Pure, network/DB-free mapping — mirrors every other adapter's _map_*_to_fixture_payload
    (e.g. balldontlie.py's NBA version), directly unit-testable against a recorded shape.
    """
    home_player, away_player = _home_away_players(match)
    p1_sets, p2_sets = _sets_won(match)  # tied to match["player1"]/["player2"] literally
    home_is_player1 = home_player is match["player1"]
    home_sets = p1_sets if home_is_player1 else p2_sets
    away_sets = p2_sets if home_is_player1 else p1_sets
    status = _map_status(match.get("match_status", ""))
    tournament = match.get("tournament") or {}

    return FixturePayload(
        external_id=_external_id(tour, match["id"]),
        league_external_id=tour,
        home_team_external_id=_external_id(tour, home_player["id"]),
        away_team_external_id=_external_id(tour, away_player["id"]),
        kickoff_utc=_match_kickoff_utc(match),
        season=str(match.get("season") or match["tournament"]["season"]),
        home_team_name=home_player.get("full_name"),
        away_team_name=away_player.get("full_name"),
        # short_name carries the FULL name, not an abbreviation. It is the cross-provider key
        # ingest_odds.py matches TheRundown's odds events on, and TheRundown abbreviates a
        # player as "F. Cobolli" while this provider stores "Flavio Cobolli" — an initial
        # cannot be recovered from a first name, so an abbreviation would never match. Left
        # empty (as it was) the matcher bails out immediately and tennis can never get odds at
        # all. Same convention football already uses, where short_name is the club's own name.
        home_team_short_name=home_player.get("full_name"),
        away_team_short_name=away_player.get("full_name"),
        status=status,
        home_score=home_sets if status == "completed" else None,
        away_score=away_sets if status == "completed" else None,
        result_type=_match_result_type(match) if status == "completed" else None,
        tournament_name=tournament.get("name"),
        tournament_surface=tournament.get("surface"),
        tournament_location=tournament.get("location"),
        kickoff_is_estimated=_match_kickoff_is_estimated(match),
    )


def _current_streak(
    completed_sorted_desc: list[dict], player_external_id: str
) -> tuple[float, float]:
    """Consecutive win/loss streak walking backward from the most recent match — exactly one
    of the two is ever positive, mirroring api_football.py:_parse_streaks' own convention.
    """
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


def _win_rate(matches: list[dict], player_external_id: str) -> float | None:
    """Plain win rate over an already-filtered, already-completed match list — the shared
    core computation behind form/H2H/surface stats, whichever subset of a player's history
    they're each called with."""
    if not matches:
        return None
    return sum(1 for m in matches if _match_winner_id(m) == player_external_id) / len(matches)


def _normalise_surface(surface: str | None) -> str | None:
    """Trim and case-fold a surface name before comparing.

    The provider returns both "Grass" and "Grass     " as real values — measured across 17,273
    completed ATP matches, 190 of 2,295 grass matches carry the padded form. An exact string
    comparison therefore splits one surface into two, so a player's grass record silently omits
    those matches from surface_win_rate and surface_streak, and h2h_win_rate_surface_home
    likewise. It fails quietly: the features still populate, just from a smaller sample than
    they claim to use.
    """
    if surface is None:
        return None
    cleaned = surface.strip().casefold()
    return cleaned or None


def _filter_by_surface(matches: list[dict], surface: str | None) -> list[dict]:
    target = _normalise_surface(surface)
    if not target:
        return []
    return [
        m for m in matches if _normalise_surface(m.get("tournament", {}).get("surface")) == target
    ]


def _filter_meetings_vs_opponent(
    matches: list[dict], player_external_id: str, opponent_external_id: str
) -> list[dict]:
    """matches is one player's own match history (as returned by /matches?player_ids[]=X) —
    filters to the subset actually played against the given opponent."""
    return [
        m
        for m in matches
        if _raw_player_id(_opponent(m, player_external_id)) == opponent_external_id
    ]


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
    logic at all), so factoring now would be premature abstraction against actual precedent.
    """
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


async def _upcoming_match_ids(
    client: httpx.AsyncClient, window_start: date, window_end: date
) -> set[str]:
    """Ids of matches in the window that have NOT started, as strings.

    Only `scheduled` counts. A live match's price is already reacting to the score, and a
    finished match's last price effectively states the winner — neither is a pre-match market.
    See fetch_odds for the measurement that made this necessary.
    """
    tournaments = await _fetch_all_pages(client, "/tournaments", {"per_page": 100})
    upcoming: set[str] = set()
    for tournament in tournaments:
        if not _tournament_overlaps_window(tournament, window_start, window_end):
            continue
        matches = await _fetch_all_pages(
            client, "/matches", {"tournament_ids[]": tournament["id"], "per_page": 100}
        )
        for match in matches:
            match.setdefault("tournament", tournament)
            if not _is_same_edition(match, tournament):
                continue
            if _map_status(match.get("match_status")) == "scheduled":
                upcoming.add(str(match["id"]))
    return upcoming


def _american_to_decimal(american: float | None) -> float | None:
    """BallDontLie quotes American odds (confirmed live: -158 / +124 on a real DraftKings row);
    the TDD requires decimal at ingest.

    Deliberately NOT imported from therundown.py despite the identical formula: that version
    also filters TheRundown's 0.0001 masked-affiliate sentinel, which is a quirk of that
    provider's subscription model and has no meaning here. Sharing it would either carry a
    misleading filter into this feed or invite someone to remove the filter and silently break
    TheRundown's.
    """
    if american is None or american == 0:
        return None
    if american > 0:
        return round(1 + american / 100, 4)
    return round(1 + 100 / abs(american), 4)


def _map_odds_row_to_payload(row: dict, tour: str) -> OddsPayload | None:
    """Pure, network-free mapping — unit-testable against a recorded real response.

    THE ORIENTATION IS THE WHOLE POINT OF THIS FUNCTION. The feed's player1/player2 slots are
    not our home/away: home is whichever player has the LOWER id (_home_away_players), and the
    provider is free to order its own slots differently — for completed matches it demonstrably
    puts the WINNER in player1, which is what caused this codebase's worst bug to date (100%
    target leakage in the first tennis training run). Reading player1_odds as "home odds"
    positionally would reintroduce exactly that class of error, this time as inverted prices
    shown to users: a 1.17 favourite displayed at 8.00. So both the price and the identity are
    resolved by id, never by position.

    Returns None rather than a half-populated payload when either price is missing — an odds
    row with no usable price is not a real market.
    """
    match_id = row.get("match_id")
    player1, player2 = row.get("player1") or {}, row.get("player2") or {}
    if match_id is None or player1.get("id") is None or player2.get("id") is None:
        return None

    # Same rule as _home_away_players, applied to the odds row's own player objects.
    p1_is_home = int(player1["id"]) < int(player2["id"])
    home_american = row.get("player1_odds") if p1_is_home else row.get("player2_odds")
    away_american = row.get("player2_odds") if p1_is_home else row.get("player1_odds")

    home_odds = _american_to_decimal(home_american)
    away_odds = _american_to_decimal(away_american)
    if home_odds is None or away_odds is None:
        return None

    updated_at = _parse_updated_at(row.get("updated_at"))
    return OddsPayload(
        # Our own tour-prefixed id, so ingest_odds._resolve_fixture matches this directly
        # against Fixture.external_id — no fuzzy team-name join for tennis at all.
        fixture_external_id=_external_id(tour, match_id),
        bookmaker=str(row.get("vendor") or "unknown"),
        market="h2h",
        home_odds=home_odds,
        draw_odds=None,  # tennis has no draw
        away_odds=away_odds,
        updated_at=updated_at,
    )


def _parse_updated_at(raw: str | None) -> datetime:
    """Falls back to now() rather than failing the row — a real price with an unreadable
    timestamp is still a real price, and this column only drives staleness display."""
    if not raw:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


async def _fetch_matches_across_seasons(
    client: httpx.AsyncClient, player_external_id: str, seasons: list[int]
) -> list[dict]:
    """/matches?player_ids[]=X with no season param only returns a thin recent window
    (confirmed live — see module docstring), so every "recent history" computation loops an
    explicit season param across TENNIS_SEASONS_BACK years instead."""
    matches: list[dict] = []
    for season in seasons:
        matches.extend(
            await _fetch_all_pages(
                client,
                "/matches",
                {"player_ids[]": player_external_id, "season": season, "per_page": 100},
            )
        )
    return matches


# A tournament's advertised start_date is the MAIN DRAW; qualifying is played before it, and
# those matches are real, ingested, and shown in the feed. Measured across five real ATP events
# — the provider's own dates against the earliest match we hold for each:
#
#     Cincinnati Open            advertised 08-13   first match 08-11   2 days early
#     National Bank Open         advertised 08-02   first match 08-01   1
#     Mifel Tennis Open          advertised 07-27   first match 07-25   2
#     Mubadala Citi DC Open      advertised 07-27   first match 07-25   2
#     Millennium Estoril Open    advertised 07-20   first match 07-18   2
#
# Three clears the observed maximum with margin. Padding the END too, since a rain-delayed
# event can finish after its advertised close and the cost of being generous is one extra
# tournament's matches per poll.
TOURNAMENT_WINDOW_PAD_DAYS = 3


def _tournament_overlaps_window(tournament: dict, window_start: date, window_end: date) -> bool:
    """Whether this tournament could have matches inside the window.

    THE PAD IS LOAD-BEARING, not defensive tidiness. Cincinnati Open advertises 08-13..08-23
    while playing qualifying from 08-11. ingest_fixtures looks 7 days ahead so it ingested
    those 24 matches happily; ingest_live_scores looks only +/-1 day, so the tournament fell
    outside its window and every one of those fixtures was frozen as SCHEDULED with no score
    for the entire day they were actually played. The fixtures were live on the provider the
    whole time — nothing was ever asked for them.
    """
    pad = timedelta(days=TOURNAMENT_WINDOW_PAD_DAYS)
    t_start = date.fromisoformat(str(tournament["start_date"])[:10]) - pad
    t_end = date.fromisoformat(str(tournament["end_date"])[:10]) + pad
    return t_start <= window_end and t_end >= window_start


class BallDontLieTennisAdapter(DataSourceAdapter):
    """ATP + WTA. fetch_fixtures deliberately does NOT assume /matches supports a date-range
    filter — confirmed live that /matches?start_date=X&end_date=Y is silently IGNORED (returns
    the same results regardless of the dates passed) — instead it lists tournaments overlapping
    the window via /tournaments (Free tier, has real start_date/end_date fields) and fetches
    /matches per tournament via the PLURAL `tournament_ids[]` param.

    CRITICAL, live-confirmed API bug: the singular `tournament_id` param (matching this
    provider's OpenAPI spec and every other single-resource filter convention in this API) is
    SILENTLY IGNORED — confirmed live with a nonsense tournament_id value returning the exact
    same (wrong) results as any real one. Only the PLURAL `tournament_ids[]` array form
    (matching player_ids[]'s own convention) actually filters correctly. Getting this wrong
    first produced a real, serious bug: every "different" tournament fetched the same
    unfiltered global match list, and a training-data collection run using the singular form
    produced a 2.66-million-row game log for what should have been ~25k real rows before this
    was caught and fixed — never trust a singular filter name without confirming it live
    against a deliberately wrong ID."""

    def __init__(self) -> None:
        self._api_key = get_settings().balldontlie_api_key

    def _client(self, tour_prefix: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=BASE_URL + tour_prefix,
            headers={"Authorization": self._api_key},
            timeout=10.0,
        )

    async def fetch_odds(
        self,
        sport: str,
        league: str,
        days_ahead: int,
        dates: list[date] | None = None,
    ) -> list[OddsPayload]:
        """Real moneyline odds, GOAT tier. Complements TheRundown rather than replacing it:
        this feed has NO totals market (confirmed live — a row carries only player1_odds/
        player2_odds), so game-totals lines still come from TheRundown alone.

        What it does bring is a direct ID join. match_id here is the SAME id space as our own
        Fixture.external_id (BallDontLie is also the tennis fixtures provider), so
        ingest_odds._resolve_fixture matches on external_id and never reaches the team-name
        fuzzy fallback that produced both missed and mis-oriented tennis prices. Vendors are
        DraftKings and FanDuel, both real and unmasked — unlike TheRundown's subscription,
        where most affiliates return the 0.0001 masked sentinel.

        ONLY NOT-YET-STARTED MATCHES ARE RETURNED, and that filter is load-bearing rather than
        tidiness. /odds?season=X returns prices for matches that have ALREADY FINISHED —
        measured on a real response: 70 of 94 rows were `finished`, 2 `in_progress`, and only
        22 `scheduled`. Those finished rows are last-traded IN-PLAY prices, so they encode the
        result: every implausible quote in that sample (1.001/74.0, 1.005/51.0 — 24 rows) sat
        on a match already decided. Ingesting them would show absurd prices against completed
        fixtures, and would poison moneyline_implied_prob with a feature that has seen the
        outcome. in_progress is excluded for the same reason.

        There is no status or date filter on /odds itself, so the upcoming set is resolved the
        same way fetch_fixtures does — tournaments overlapping the window, then their matches.
        That costs about one call per overlapping tournament (typically ~5), not one per
        priced match.
        """
        tour_prefix = _tour_prefix(league)
        season = _current_tennis_season()
        now = datetime.now(UTC)
        window_start, window_end = now.date(), (now + timedelta(days=days_ahead)).date()

        async with self._client(tour_prefix) as client:
            rows = await _fetch_all_pages(client, "/odds", {"season": season, "per_page": 100})
            if not rows:
                return []
            upcoming = await _upcoming_match_ids(client, window_start, window_end)

        payloads: list[OddsPayload] = []
        for row in rows:
            if str(row.get("match_id")) not in upcoming:
                continue
            payload = _map_odds_row_to_payload(row, league)
            if payload is not None:
                payloads.append(payload)
        return payloads

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
                    client,
                    "/matches",
                    {"tournament_ids[]": tournament["id"], "per_page": 100},
                )
                for match in matches:
                    match.setdefault("tournament", tournament)
                    if not _is_same_edition(match, tournament):
                        continue
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
        current_season = _current_tennis_season()
        seasons = [current_season - i for i in range(TENNIS_SEASONS_BACK)]
        async with self._client(prefix) as client:
            matches = await _fetch_matches_across_seasons(client, raw_player_id, seasons)
            rankings = await _fetch_all_pages(
                client, "/rankings", {"player_ids[]": raw_player_id, "per_page": 100}
            )
        return _compute_team_stats(raw_player_id, matches, rankings, n_matches)

    async def fetch_injuries(self, sport: str) -> list[InjuryUpdate]:
        """No tennis injury feed at MVP — same as every non-NBA/football sport today."""
        return []


async def fetch_match_surface(tour: str, match_external_id: str) -> str | None:
    """Standalone, not part of the ABC — one live call to /matches/{id} (real, confirmed shape:
    {"data": {...}}) to read the CURRENT fixture's own tournament.surface. Called once by
    tennis_features.py:assemble_from_live_db and the result is threaded into fetch_h2h_stats/
    fetch_surface_stats below, rather than each independently re-fetching it. external_id arg
    is OUR tour-prefixed id; stripped internally."""
    api_key = get_settings().balldontlie_api_key
    prefix = _tour_prefix(tour)
    raw_match_id = _strip_tour_prefix(match_external_id)

    async with httpx.AsyncClient(
        base_url=BASE_URL + prefix, headers={"Authorization": api_key}, timeout=10.0
    ) as client:
        response = await _get_with_retry(client, f"/matches/{raw_match_id}", {})
    return response.json().get("data", {}).get("tournament", {}).get("surface")


async def fetch_h2h_stats(
    tour: str, player_external_id: str, opponent_external_id: str, surface: str | None
) -> tuple[float | None, float | None]:
    """Returns (h2h_win_rate_overall, h2h_win_rate_on_surface) between two players, computed
    from ONE fetch of their shared match history (mirrors api_football.py:fetch_h2h_stats'
    combined-metrics-from-one-call pattern, rather than the earlier version's separate overall-
    only helper). Derived manually from the player's own match history rather than the
    GOAT-gated /head_to_head endpoint, so this stays reachable at ALL-STAR tier alone. Loops
    TENNIS_SEASONS_BACK seasons (see module docstring — an unfiltered call only returns a thin
    recent window). external_id args are OUR tour-prefixed ids; stripped internally."""
    prefix = _tour_prefix(tour)
    raw_player_id = _strip_tour_prefix(player_external_id)
    raw_opponent_id = _strip_tour_prefix(opponent_external_id)
    current_season = _current_tennis_season()
    seasons = [current_season - i for i in range(TENNIS_SEASONS_BACK)]

    api_key = get_settings().balldontlie_api_key
    async with httpx.AsyncClient(
        base_url=BASE_URL + prefix, headers={"Authorization": api_key}, timeout=10.0
    ) as client:
        matches = await _fetch_matches_across_seasons(client, raw_player_id, seasons)

    completed = [m for m in matches if _match_completed(m)]
    meetings = _filter_meetings_vs_opponent(completed, raw_player_id, raw_opponent_id)
    overall = _win_rate(meetings, raw_player_id)
    on_surface = _win_rate(_filter_by_surface(meetings, surface), raw_player_id)
    return overall, on_surface


async def fetch_surface_stats(
    tour: str, player_external_id: str, surface: str | None
) -> tuple[float | None, float | None]:
    """Returns (surface_win_rate, surface_streak) for player_external_id, computed from ONE
    fetch of their own match history filtered to the given surface — surface stats are
    fixture-specific (the CURRENT tournament's surface), which doesn't fit
    fetch_team_stats' per-team-per-ingest-run cache (see
    app/workers/ingest_fixtures.py's team_stats_cache). The surface itself is passed in
    (from fetch_match_surface, called once by the caller) rather than re-fetched here, to
    avoid a redundant /matches/{id} call when both this and fetch_h2h_stats need it for the
    same fixture. Loops TENNIS_SEASONS_BACK seasons (see module docstring). external_id arg
    is OUR tour-prefixed id; stripped internally."""
    if not surface:
        return None, None
    prefix = _tour_prefix(tour)
    raw_player_id = _strip_tour_prefix(player_external_id)
    current_season = _current_tennis_season()
    seasons = [current_season - i for i in range(TENNIS_SEASONS_BACK)]

    api_key = get_settings().balldontlie_api_key
    async with httpx.AsyncClient(
        base_url=BASE_URL + prefix, headers={"Authorization": api_key}, timeout=10.0
    ) as client:
        matches = await _fetch_matches_across_seasons(client, raw_player_id, seasons)

    completed = [m for m in matches if _match_completed(m)]
    same_surface = _filter_by_surface(completed, surface)
    same_surface.sort(key=_match_date, reverse=True)
    win_rate = _win_rate(same_surface, raw_player_id)
    streak, _losing_streak = (
        _current_streak(same_surface, raw_player_id) if same_surface else (None, None)
    )
    return win_rate, streak


# Serve/return measures shown in the H2H panel, in display order. Chosen from what
# /match_stats actually returns (probed live 2026-08-13) for what a bettor reads a matchup on:
# how well each player holds serve, and how well they break. Percentages arrive already scaled
# 0-100, so they are shown as-is with a "%" suffix rather than re-derived.
H2H_PANEL_FIELDS = (
    ("aces", "Aces", ""),
    ("double_faults", "Double faults", ""),
    ("first_serve_pct", "1st serve", "%"),
    ("first_serve_points_won_pct", "1st serve won", "%"),
    ("break_points_converted_pct", "Break points won", "%"),
    ("total_points_won_pct", "Total points won", "%"),
)

# Whole-match rows carry set_number 0; per-set rows repeat the same match. Averaging without
# this filter would weight a five-setter five times and double-count every number.
WHOLE_MATCH_SET_NUMBER = 0

H2H_PANEL_MEETINGS = 5


async def fetch_h2h_panel(
    home_external_id: str, away_external_id: str, league: str = "atp"
) -> H2HPanel | None:
    """Head-to-head history for the fixture detail panel: the career record plus serve and
    return averages over their most recent meetings.

    Uses the DEDICATED /head_to_head endpoint for the record -- one call, and it returns
    player1_wins/player2_wins directly rather than needing a match history to be searched the
    way basketball does. Both endpoints are GOAT-tier and this account has GOAT for ATP.

    Returns None rather than an empty record when the two have never met, and degrades to the
    record alone if /match_stats is unavailable -- a panel with a real record and no stat rows
    is honest; one with fabricated averages is not.
    """
    api_key = get_settings().balldontlie_api_key
    home_raw = _strip_tour_prefix(home_external_id)
    away_raw = _strip_tour_prefix(away_external_id)

    async with httpx.AsyncClient(
        base_url=f"https://api.balldontlie.io{_tour_prefix(league)}",
        headers={"Authorization": api_key},
        timeout=15.0,
    ) as client:
        try:
            response = await _get_with_retry(
                client, "/head_to_head", {"player1_id": home_raw, "player2_id": away_raw}
            )
        except httpx.HTTPStatusError as exc:
            # A pair who have NEVER MET comes back 404, not an empty record -- confirmed live
            # against two real upcoming ATP players. That is a normal answer for a first-time
            # meeting, which is common in early rounds, so it must not surface as an error or
            # be logged as one.
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise
        record = (response.json() or {}).get("data") or {}
        if not record:
            return None
        # The endpoint answers in ITS OWN player1/player2 order, which is not our home/away.
        # Reading the wins without checking which slot our home player landed in would invert
        # the record for roughly half of all fixtures -- the same class of error as trusting
        # teams_normalized order for home/away.
        p1_is_home = str((record.get("player1") or {}).get("id")) == str(home_raw)
        home_wins = record.get("player1_wins" if p1_is_home else "player2_wins") or 0
        away_wins = record.get("player2_wins" if p1_is_home else "player1_wins") or 0
        if home_wins + away_wins == 0:
            return None

        stats: list[H2HPanelStat] = []
        try:
            meetings = await _recent_meetings(client, home_raw, away_raw)
            stats = await _serve_return_averages(client, meetings, home_raw, away_raw)
        except httpx.HTTPError:
            # The record is already real and worth showing on its own.
            pass

    return H2HPanel(
        meetings_count=home_wins + away_wins,
        home_wins=int(home_wins),
        # Tennis cannot draw -- structurally zero, not unmeasured.
        draws=0,
        away_wins=int(away_wins),
        stats=stats,
    )


async def _recent_meetings(client, home_raw: str, away_raw: str) -> list[dict]:
    """Their most recent completed meetings, newest first."""
    current_season = _current_tennis_season()
    seasons = [current_season - i for i in range(TENNIS_SEASONS_BACK)]
    matches = await _fetch_matches_across_seasons(client, home_raw, seasons)
    meetings = [
        m
        for m in matches
        if _match_completed(m) and str((_opponent(m, home_raw) or {}).get("id")) == str(away_raw)
    ]
    meetings.sort(key=lambda m: str(_match_date(m)), reverse=True)
    return meetings[:H2H_PANEL_MEETINGS]


async def _serve_return_averages(
    client, meetings: list[dict], home_raw: str, away_raw: str
) -> list[H2HPanelStat]:
    if not meetings:
        return []
    response = await _get_with_retry(
        client, "/match_stats", {"match_ids[]": [m["id"] for m in meetings]}
    )
    rows = [
        row
        for row in (response.json() or {}).get("data", [])
        if row.get("set_number") == WHOLE_MATCH_SET_NUMBER
    ]
    by_player: dict[str, list[dict]] = {}
    for row in rows:
        by_player.setdefault(str((row.get("player") or {}).get("id")), []).append(row)

    def average(player_raw: str, field: str) -> float | None:
        values = [r[field] for r in by_player.get(str(player_raw), []) if r.get(field) is not None]
        # Each field averages over ITS OWN non-null values: a meeting missing one measure still
        # contributes to the others, and a measure missing everywhere stays None rather than
        # becoming a fabricated zero.
        return round(sum(values) / len(values), 1) if values else None

    stats = []
    for field, label, suffix in H2H_PANEL_FIELDS:
        home_value, away_value = average(home_raw, field), average(away_raw, field)
        if home_value is None and away_value is None:
            continue
        stats.append(H2HPanelStat(label, home_value, away_value, suffix))
    return stats
