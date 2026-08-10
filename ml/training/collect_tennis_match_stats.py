"""Collect real ATP per-match serve/return statistics from BallDontLie's /match_stats.

This endpoint is GOAT-tier and returned 401 until that plan was activated, which is why the
tennis model shipped without any serve features and why the total-games viability analysis
(ml/export/SportIQ_Tennis_Games_Viability.ipynb) could only measure rank and surface.

Two things make this far cheaper than the equivalent football collection, both confirmed live:

  * /match_stats paginates BY SEASON, not per fixture. Football's lineups and corners cost one
    call PER FIXTURE (~9,600 calls for three leagues); a whole ATP season here is ~200 pages.
  * GOAT advertises 600 req/min, ten times ALL-STAR's 60.

So five seasons is minutes of wall-clock, not a multi-day staged collection.

THE season FILTER ON /match_stats IS SILENTLY IGNORED. Confirmed live against a deliberately
nonsensical value: season=2021, season=1899, seasons[]=2021 and no filter at all return the
identical unfiltered stream (starting at 2026). /matches?season=X does filter correctly, so
this is per-endpoint, not a house style. Looping seasons here therefore refetches the entire
history once per season -- the first version of this script did exactly that and was killed
mid-run. One pass collects everything; callers filter locally.

set_number 0 is the whole-match aggregate; 1/2/3 are individual sets. BOTH ARE COLLECTED, BUT
PER-SET ROWS EXIST ONLY FOR 2026 -- measured across the full 138,219-row collection, every
one of the 9,702 per-set rows falls in the current season and there are exactly zero for
2021-2025. An early 100-row sample suggested otherwise; it was drawn from the head of the
unfiltered stream, which is 2026 data. So the whole-match aggregate is what any historical
model can actually train on, and a first-set market remains unsupported historically.

Checkpoints as it goes, because a rank-points run once crashed at 11,000/17,008 lookups with
nothing saved and had to start over.

    backend/.venv/Scripts/python ml/training/collect_tennis_match_stats.py
"""

import asyncio
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

# Must precede any import that reads settings — pydantic-settings resolves env_file against
# the process CWD, so running this from the repo root would otherwise load a blank .env and
# fail with an empty API key that looks exactly like a real auth failure.
load_dotenv(BACKEND_DIR / ".env")

from collect_tennis_data import (  # noqa: E402
    SEASONS,
    TOUR,
    _client,
    _get_with_retry,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / f"tennis_match_stats_{TOUR}.parquet"

# ~8 rows per match (2 players x up to 4 set_numbers) means a season runs to a few hundred
# pages -- collect_tennis_data.py's MAX_PAGES=150 is sized for /matches (1 row per match) and
# would silently truncate here, so this gets its own, much larger ceiling.
MAX_PAGES = 1500
PER_PAGE = 100

# 600/min advertised; 0.12s is ~500/min, leaving headroom for retries rather than riding the
# ceiling. A 429 on this provider escalates into spurious 401s under burst load.
REQUEST_DELAY_SECONDS = 0.12

# Pulled out explicitly rather than dumping whatever keys happen to come back, so a provider
# adding or renaming a field shows up as a missing column instead of silently changing schema.
STAT_FIELDS = (
    "serve_rating",
    "aces",
    "double_faults",
    "first_serve_pct",
    "first_serve_points_won_pct",
    "second_serve_points_won_pct",
    "break_points_saved_pct",
    "return_rating",
    "first_return_won_pct",
    "second_return_won_pct",
    "break_points_converted_pct",
    "total_service_points_won_pct",
    "total_return_points_won_pct",
    "total_points_won_pct",
)


def _flatten(row: dict) -> dict | None:
    """One flat record per (match, player, set_number).

    Returns None if the row carries no usable identity — a stat row we cannot attach to both a
    match and a player is unjoinable, and keeping it would only inflate the row count.
    """
    match = row.get("match") or {}
    player = row.get("player") or {}
    match_id, player_id = match.get("id"), player.get("id")
    if match_id is None or player_id is None:
        return None
    return {
        "MATCH_ID": str(match_id),
        "PLAYER_ID": str(player_id),
        "SET_NUMBER": row.get("set_number"),
        "SEASON": match.get("season"),
        "TOURNAMENT_ID": match.get("tournament_id"),
        "ROUND": match.get("round"),
        "WINNER_ID": (str(match["winner_id"]) if match.get("winner_id") is not None else None),
        **{field.upper(): row.get(field) for field in STAT_FIELDS},
    }


async def _collect_all(client) -> tuple[list[dict], bool]:
    """One unfiltered pass over /match_stats. Returns (rows, truncated).

    No season parameter is sent at all — see the module docstring for the live confirmation
    that it is ignored. Sending one anyway would imply a scoping that does not exist.
    """
    rows: list[dict] = []
    cursor = None
    truncated = True
    for page in range(MAX_PAGES):
        params = {"per_page": PER_PAGE}
        if cursor is not None:
            params["cursor"] = cursor
        response = await _get_with_retry(client, "/match_stats", params)
        payload = response.json()
        rows.extend(r for r in map(_flatten, payload.get("data", [])) if r is not None)
        cursor = payload.get("meta", {}).get("next_cursor")
        if page and page % 100 == 0:
            print(f"    page {page}: {len(rows):,} rows", flush=True)
            # Checkpoint mid-run: a crash at page 1,300 should not cost the whole pass.
            pd.DataFrame(rows).to_parquet(OUT_PATH, index=False)
        if not cursor:
            truncated = False  # cursor genuinely exhausted — the pass is complete
            break
        await asyncio.sleep(REQUEST_DELAY_SECONDS)
    return rows, truncated


async def main_async() -> None:
    if OUT_PATH.exists():
        existing = pd.read_parquet(OUT_PATH)
        print(f"  {OUT_PATH.name} already exists ({len(existing):,} rows) — delete to refetch")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with _client() as client:
        rows, truncated = await _collect_all(client)

    frame = pd.DataFrame(rows)
    if frame.empty:
        raise SystemExit("no match stats collected")
    # Belt and braces: one pass should not repeat a (match, player, set), but a cursor that
    # loops would show up here rather than silently inflating every downstream average.
    before = len(frame)
    frame = frame.drop_duplicates(["MATCH_ID", "PLAYER_ID", "SET_NUMBER"])
    frame.to_parquet(OUT_PATH, index=False)

    per_set = frame[frame.SET_NUMBER != 0]
    print(
        f"\n  wrote {len(frame):,} rows -> {OUT_PATH.name}  ({before - len(frame):,} dupes dropped)"
    )
    print(
        f"  {frame.MATCH_ID.nunique():,} distinct matches, seasons "
        f"{int(frame.SEASON.min())}-{int(frame.SEASON.max())}"
    )
    print(f"  aggregate (set 0) rows : {len(frame) - len(per_set):,}")
    print(
        f"  per-set rows           : {len(per_set):,} "
        f"(seasons {sorted(per_set.SEASON.dropna().unique().tolist())})"
    )
    in_window = frame[frame.SEASON.between(min(SEASONS), max(SEASONS))]
    print(
        f"  training window {min(SEASONS)}-{max(SEASONS)}: {in_window.MATCH_ID.nunique():,} matches"
    )
    if truncated:
        raise SystemExit(
            f"MAX_PAGES={MAX_PAGES} reached with a cursor still pending — data is TRUNCATED. "
            "Raise MAX_PAGES and rerun; do not train on this file."
        )


def main() -> None:
    # One asyncio.run for the entire script — two in a single process corrupt the shared
    # engine/session factory on Windows (see CLAUDE.md's train_nba.py note).
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
