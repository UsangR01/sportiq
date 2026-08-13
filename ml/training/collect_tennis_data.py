"""One-off historical data collection for tennis model training.

ATP only, by design — not a temporary placeholder: the user's real BallDontLie subscription
(confirmed live) is ALL-STAR for ATP only, WTA still 401s on /matches until that tour is
separately subscribed. Re-run this unchanged once WTA is confirmed (just point TOUR at
"wta") — nothing else about the shape below is ATP-specific.

Pulls each season directly via /matches?season=X (confirmed live, working back to at least
2021, and — unlike /matches?player_ids[]=X with no season param, which only returns a thin
~3.5-month recent window — this correctly returns the FULL season's real matches). No
/tournaments call is needed here at all: each match response already embeds its own
`tournament` object (id, surface, dates), so surface comes along for free.

CRITICAL, live-confirmed API bug this script does NOT hit (see
app/adapters/balldontlie_tennis.py's module docstring for the full story, since the LIVE
adapter's fetch_fixtures does need per-tournament filtering and hit this directly): the
singular `tournament_id` query param on /matches is silently IGNORED by the real API — it
returns the same unfiltered global match list regardless of the value passed, confirmed with
a nonsense ID. Only the plural `tournament_ids[]` array form actually filters. This script
sidesteps the whole question by pulling per-SEASON instead of per-tournament, which the
`season` param (confirmed correct, unlike `start_date`/`end_date`, also silently ignored) is
built for.

Game log shape: one row per player per completed match (MATCH_ID, TOURNAMENT_ID, SEASON,
PLAYER_ID, OPPONENT_ID, HOME_AWAY, GAME_DATE, WL, SURFACE) — exactly what
app/models_ml/tennis_features.py:assemble_from_game_log expects. H2H, H2H-on-surface, and
surface win-rate/streak features are ALL derived directly from this same game log at training
time (no separate H2H collection step needed — see that module).

HOME_AWAY is assigned via app/adapters/balldontlie_tennis.py:_home_away_players (a stable,
outcome-independent tiebreak by player id) — NOT match["player1"]/["player2"] directly. A
real, serious bug, found only after the first training run: BallDontLie always lists the
eventual WINNER as player1 for a completed match (confirmed live, 20/20 in a sampled batch),
so the original naive player1="home" mapping baked 100% target leakage into every label —
train (2021-2023) and validation (2024) came back 100% "home won" (test/2025 was "only" 73%),
which crashed XGBoost outright ("Invalid classes inferred... Expected: [0], got [1]") since a
single-class y can't even be fit. See that adapter function's own docstring for the full
live-verification story.

Real historical point-in-time RANKINGS (for leakage-safe rank_diff) need their own collection
pass: confirmed live that /rankings?player_ids[]=X with no `date` param only returns the
CURRENT ranking (a single row), and /rankings?season=X with no player filter silently ignores
`season` and also only returns the current top-500 — genuine historical point-in-time lookups
require an explicit `date=YYYY-MM-DD` query (confirmed live: this correctly resolves to real
rankings as of the most recent weekly snapshot on/before that date). Rather than one call per
(player, match) — the same call repeated for every match a player plays within one tournament
week — this caches by (player_id, ISO Monday of the match's week), since a real ranking
snapshot only changes weekly.

BATCHED by week, not one call per (player, week): the official BallDontLie API PDF
(balldontlie_api_documentation.pdf, added to the repo root by the user) documents
/rankings' `player_ids` query param as an `array` type, combinable with `date` — confirmed
live that a single call with several `player_ids[]` values plus one `date` returns real,
distinct per-player rows in one round trip. For this dataset that's 153 distinct weeks
(vs. 17,008 individual (player, week) pairs — a ~60-110x reduction), chunked to
RANK_BATCH_SIZE players per call to stay within the API's own per_page ceiling. A player
requested but absent from the response (e.g. unranked/outside the tracked field at that
date) is recorded as RANK_POINTS=None — never fabricated as 0.

Real, live-confirmed root cause of the original ~7-hour crawl's near-constant 429s: this
script previously fired one request per (player, week) back-to-back with NO proactive
pacing, only backing off after a 429 already happened — on the documented ALL-STAR rate
limit (60 req/min per the official PDF), a burst with no inter-request delay exhausts the
whole per-minute allowance in under a second, then the whole next request waits out an
entire ~50s cooldown window, repeating forever. RANK_REQUEST_DELAY_SECONDS now paces every
real request proactively (mirrors TheRundown's own ODDS_REQUEST_DELAY_SECONDS precedent in
collect_football_data.py) so the documented budget is what actually gets used, not burned in
one burst per minute. Also separately confirmed live: a stale/duplicate copy of this same
script left running in the background (the exact "duplicate process" footgun already
documented elsewhere in this project) was itself consuming the rate budget during
diagnosis — always confirm no other collect_tennis_data.py process is still alive before
troubleshooting a "why am I still being throttled" symptom.

No historical odds are collected here (predictions ship first, tennis odds are an explicit
fast-follow — see CLAUDE.md) — moneyline_implied_prob_home is always None for every training
example, and the trained model's flat-stake ROI metric is therefore always None too, an
honest gap rather than a fabricated figure.

Caches to local parquet under ml/data/, so re-running train_tennis.py doesn't re-hit the API.

Usage (from repo root):
    backend/.venv/Scripts/python ml/training/collect_tennis_data.py
"""

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
import pandas as pd
from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
load_dotenv(BACKEND_DIR / ".env")  # see collect_nba_data.py for why this is needed explicitly

from app.adapters.balldontlie_tennis import (  # noqa: E402
    _home_away_players,
    _match_completed,
    _match_date,
    _match_winner_id,
)
from app.core.config import get_settings  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

TOUR = "atp"
BASE_URL = f"https://api.balldontlie.io/{TOUR}/v1"

# Mirrors football's 5-season window (train=[2021,2022,2023], validate=2024, test=2025) —
# confirmed live that /tournaments?season=X works back to at least 2021. Tennis seasons are
# plain calendar-year ints, unlike NBA's "2020-21" strings.
SEASONS = [2021, 2022, 2023, 2024, 2025]

MAX_RETRIES = 5
MAX_PAGES = 150  # a full ATP season (main draw + qualifying across ~60 tournaments) can
# genuinely run into the low thousands of matches — confirmed live a single Grand Slam alone
# returned 500+ rows, so 150 pages * 100/page = up to 15,000 matches/season, comfortably above
# any real season's total.

RANK_BATCH_SIZE = 100  # matches the API's own per_page ceiling (confirmed live/via the
# official PDF) — the largest single /rankings?player_ids[]=...&date=... call the endpoint
# reliably answers in one page.
RANK_REQUEST_DELAY_SECONDS = 1.1  # proactive pacing to stay under ALL-STAR's documented
# 60 req/min — see module docstring for why this, not just retry-after-429, is the real fix.


async def _get_with_retry(client: httpx.AsyncClient, path: str, params: dict) -> httpx.Response:
    """Mirrors app/adapters/balldontlie_tennis.py:_get_with_retry — reimplemented here since
    this is a one-off script outside the adapter layer, matching collect_football_data.py's
    own precedent for not sharing HTTP helpers across the adapter/training boundary.

    Also retries on httpx.TransportError (a real ReadTimeout crashed a ~7-hour rank-points
    collection run at 11000/17008 lookups, with no partial-progress checkpoint to resume
    from) — the original version only retried on 429, so a single transient network hiccup
    was an unhandled crash, not a real reason to lose that much real, already-fetched work.
    """
    response = None
    for attempt in range(MAX_RETRIES):
        try:
            response = await client.get(path, params=params)
        except httpx.TransportError as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            delay = min(2**attempt, 30)
            print(f"  {type(exc).__name__} on {path}, retrying in {delay:.0f}s")
            await asyncio.sleep(delay)
            continue
        if response.status_code == 429 and attempt < MAX_RETRIES - 1:
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else min(2**attempt, 30)
            print(f"  429 on {path}, backing off {delay:.0f}s")
            await asyncio.sleep(delay)
            continue
        response.raise_for_status()
        return response
    response.raise_for_status()
    return response


async def _fetch_all_pages(client: httpx.AsyncClient, path: str, params: dict) -> list[dict]:
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


def _client() -> httpx.AsyncClient:
    api_key = get_settings().balldontlie_api_key
    return httpx.AsyncClient(base_url=BASE_URL, headers={"Authorization": api_key}, timeout=20.0)


async def collect_game_log() -> pd.DataFrame:
    """One row per player per completed match, across every real ATP match in each season in
    SEASONS — a direct /matches?season=X paginated sweep, confirmed live to return real,
    correctly-scoped, non-duplicated results (unlike the tournament_id singular filter — see
    module docstring). Each match's own embedded tournament object already carries surface, so
    no separate /tournaments call is needed."""
    rows = []
    async with _client() as client:
        for season in SEASONS:
            print(f"fetching {season} matches...")
            matches = await _fetch_all_pages(
                client, "/matches", {"season": season, "per_page": 100}
            )
            print(f"  {len(matches)} matches")

            completed = 0
            for match in matches:
                if not _match_completed(match):
                    continue
                completed += 1
                game_date = _match_date(match)
                tournament = match.get("tournament", {})
                # STRIPPED, because the provider is not consistent about it. Measured over the
                # real collected log: "Grass" appears 4,296 times and "Grass " — same value with
                # a trailing space — another 386. Unstripped they are two distinct categories,
                # which silently splits any per-surface cut and, worse, splits the surface
                # FEATURES: surface_win_rate / surface_streak / h2h_win_rate_surface all match on
                # this string, so 8% of grass matches were being compared against the wrong pool.
                surface = (tournament.get("surface") or "").strip() or None
                winner_id = _match_winner_id(match)

                home_player, away_player = _home_away_players(match)
                for player, opponent, home_away in (
                    (home_player, away_player, "home"),
                    (away_player, home_player, "away"),
                ):
                    player_id = str(player["id"])
                    rows.append(
                        {
                            "MATCH_ID": match["id"],
                            "TOURNAMENT_ID": tournament.get("id"),
                            "SEASON": season,
                            "PLAYER_ID": player_id,
                            "OPPONENT_ID": str(opponent["id"]),
                            "HOME_AWAY": home_away,
                            "GAME_DATE": game_date,
                            "WL": "W" if winner_id == player_id else "L",
                            "SURFACE": surface,
                        }
                    )
            print(f"  {completed} completed matches")
            await asyncio.sleep(0.3)  # polite pacing, mirrors collect_nba_data.py

    return pd.DataFrame(
        rows,
        columns=[
            "MATCH_ID",
            "TOURNAMENT_ID",
            "SEASON",
            "PLAYER_ID",
            "OPPONENT_ID",
            "HOME_AWAY",
            "GAME_DATE",
            "WL",
            "SURFACE",
        ],
    )


def _iso_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def collect_rank_points(games: pd.DataFrame, checkpoint_path: Path) -> pd.DataFrame:
    """Real, point-in-time ranking points for every distinct (player, ISO week) combination
    that appears in the collected game log.

    Batched by WEEK, not one call per (player, week) pair — see module docstring for the real,
    live-confirmed /rankings?player_ids[]=...&date=... batching this relies on. All players
    needing the same week's ranking are looked up together, chunked to RANK_BATCH_SIZE per
    call — for this game log, 153 distinct weeks replace 17,008 individual lookups.

    Resumable: if checkpoint_path already has partial progress from an earlier, crashed run,
    already-completed WEEKs are skipped entirely rather than re-queried."""
    distinct = games[["PLAYER_ID", "GAME_DATE"]].drop_duplicates().copy()
    distinct["WEEK"] = distinct["GAME_DATE"].apply(_iso_monday)
    by_week = distinct.groupby("WEEK")["PLAYER_ID"].apply(lambda s: sorted(set(s))).to_dict()
    weeks = sorted(by_week.keys())

    rows: list[dict] = []
    done_weeks: set = set()
    if checkpoint_path.exists():
        checkpoint_df = pd.read_parquet(checkpoint_path)
        rows = checkpoint_df.to_dict("records")
        done_weeks = set(checkpoint_df["WEEK"].unique())
        weeks = [w for w in weeks if w not in done_weeks]
        print(
            f"resuming from checkpoint: {len(done_weeks)} weeks already fetched, "
            f"{len(weeks)} remaining"
        )

    print(
        f"fetching real point-in-time rankings for {len(weeks)} distinct weeks "
        f"({sum(len(by_week[w]) for w in weeks)} player-week lookups)..."
    )

    async with _client() as client:
        for i, week in enumerate(weeks):
            player_ids = by_week[week]
            points_by_player: dict[str, float | None] = {}
            for batch in _chunk(player_ids, RANK_BATCH_SIZE):
                response = await _get_with_retry(
                    client,
                    "/rankings",
                    {
                        "player_ids[]": batch,
                        "date": week.isoformat(),
                        "per_page": RANK_BATCH_SIZE,
                    },
                )
                for entry in response.json().get("data", []):
                    points_by_player[str(entry["player"]["id"])] = entry.get("points")
                await asyncio.sleep(RANK_REQUEST_DELAY_SECONDS)

            for player_id in player_ids:
                rows.append(
                    {
                        "PLAYER_ID": player_id,
                        "WEEK": week,
                        "RANK_POINTS": points_by_player.get(player_id),
                    }
                )

            if (i + 1) % 10 == 0 or (i + 1) == len(weeks):
                print(f"  fetched {i + 1}/{len(weeks)} weeks")
                pd.DataFrame(rows, columns=["PLAYER_ID", "WEEK", "RANK_POINTS"]).to_parquet(
                    checkpoint_path, index=False
                )

    return pd.DataFrame(rows, columns=["PLAYER_ID", "WEEK", "RANK_POINTS"])


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    games_path = DATA_DIR / f"tennis_game_log_{TOUR}.parquet"
    if games_path.exists():
        print(f"{games_path} already exists, skipping BallDontLie re-fetch")
        games = pd.read_parquet(games_path)
    else:
        games = asyncio.run(collect_game_log())
        games.to_parquet(games_path, index=False)
        print(f"saved {len(games)} game-log rows to {games_path}")

    ranks_path = DATA_DIR / f"tennis_rank_points_{TOUR}.parquet"
    checkpoint_path = DATA_DIR / f"tennis_rank_points_{TOUR}.checkpoint.parquet"
    if ranks_path.exists():
        print(f"{ranks_path} already exists, skipping BallDontLie re-fetch")
    else:
        ranks = asyncio.run(collect_rank_points(games, checkpoint_path))
        ranks.to_parquet(ranks_path, index=False)
        checkpoint_path.unlink(missing_ok=True)
        print(f"saved {len(ranks)} rank-point rows to {ranks_path}")


if __name__ == "__main__":
    main()
