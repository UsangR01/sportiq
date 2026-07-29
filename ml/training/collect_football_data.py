"""One-off historical data collection for football model training (TDD §3.2/§3.5), scoped to
EPL only for the first real trained model (see CLAUDE.md's scope decision) — the other 4
leagues are seeded and generically supported but not yet part of training data collection.

Mirrors ml/training/collect_nba_data.py's structure: fetches real fixtures/results (this
script's own game log, not nba_api — API-Football is football's fixtures/stats source),
real per-fixture lineups (the "box score" for the historical key-player-availability backtest
label — used ONLY for that label, never live, same leakage-guard separation as NBA), and a
bounded real TheRundown odds sample.

Caches to local parquet under ml/data/ so re-running train_football.py doesn't re-hit either
API.

Usage (from repo root):
    backend/.venv/Scripts/python ml/training/collect_football_data.py
"""

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd
from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
load_dotenv(BACKEND_DIR / ".env")  # see collect_nba_data.py for why this is needed explicitly

from app.adapters.api_football import BASE_URL as FOOTBALL_BASE_URL  # noqa: E402
from app.adapters.api_football import LEAGUE_IDS  # noqa: E402
from app.adapters.therundown import (  # noqa: E402
    BASE_URL as RUNDOWN_BASE_URL,
)
from app.adapters.therundown import (  # noqa: E402
    RAPIDAPI_HOST,
    _map_event_to_odds_payloads,
)
from app.core.config import get_settings  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

TARGET_LEAGUE_SLUG = "epl"
TARGET_LEAGUE_ID = LEAGUE_IDS[TARGET_LEAGUE_SLUG]
RUNDOWN_EPL_SPORT_ID = 11

# Same 5-season window ml/training/compute_football_key_players.py uses.
SEASONS = [2021, 2022, 2023, 2024, 2025]

MAX_RETRIES = 5


async def _get_with_retry(client: httpx.AsyncClient, path: str, params: dict) -> httpx.Response:
    """Mirrors app/adapters/balldontlie.py:_get_with_retry's retry-on-429 shape — this is a
    one-off script outside the adapter layer, so the same minimal pattern is reimplemented
    here rather than shared, matching collect_nba_data.py's own precedent."""
    response = None
    for attempt in range(MAX_RETRIES):
        response = await client.get(path, params=params)
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


def _football_client() -> httpx.AsyncClient:
    api_key = get_settings().api_football_key
    return httpx.AsyncClient(
        base_url=FOOTBALL_BASE_URL, headers={"x-apisports-key": api_key}, timeout=20.0
    )


async def _fetch_team_codes(client: httpx.AsyncClient, season: int) -> dict[str, str]:
    """team external id (str) -> API-Football's own 3-letter `code` field — used as a
    best-effort join key against TheRundown's teams_normalized.abbreviation for the odds
    sample below. Not verified to match TheRundown's convention exactly for every club (the
    codebase's existing CLAUDE.md note about abbreviation consistency only ever confirmed
    BallDontLie vs TheRundown for NBA) — a best-effort match, not a guaranteed one; unmatched
    fixtures simply get no real odds/moneyline feature, same graceful-miss behavior as every
    other cross-provider join in this codebase."""
    response = await client.get("/teams", params={"league": TARGET_LEAGUE_ID, "season": season})
    response.raise_for_status()
    return {
        str(row["team"]["id"]): row["team"]["code"]
        for row in response.json().get("response", [])
        if row["team"].get("code")
    }


async def collect_game_log() -> tuple[pd.DataFrame, dict[str, str]]:
    """One row per team per completed EPL fixture — the football analogue of
    collect_nba_data.py's leaguegamelog pull, built from API-Football's /fixtures rather than
    nba_api (football has no equivalent free/offline bulk endpoint). Also returns a
    team_id -> code mapping (see _fetch_team_codes) for the odds-matching step below."""
    rows = []
    team_codes: dict[str, str] = {}
    async with _football_client() as client:
        for season in SEASONS:
            team_codes.update(await _fetch_team_codes(client, season))
            print(f"fetching EPL {season}-{season + 1} fixtures...")
            response = await _get_with_retry(
                client,
                "/fixtures",
                {"league": TARGET_LEAGUE_ID, "season": season, "status": "FT-AET-PEN"},
            )
            fixtures = response.json().get("response", [])
            print(f"  {len(fixtures)} completed fixtures")

            for fx in fixtures:
                fixture_id = fx["fixture"]["id"]
                game_date = datetime.fromisoformat(
                    fx["fixture"]["date"].replace("Z", "+00:00")
                ).date()
                home = fx["teams"]["home"]
                away = fx["teams"]["away"]
                home_goals = fx["goals"]["home"]
                away_goals = fx["goals"]["away"]
                if home_goals is None or away_goals is None:
                    continue

                def wdl(gf: int, ga: int) -> str:
                    if gf > ga:
                        return "W"
                    if gf < ga:
                        return "L"
                    return "D"

                rows.append(
                    {
                        "SEASON": season,
                        "FIXTURE_ID": fixture_id,
                        "GAME_DATE": game_date,
                        "TEAM_ID": str(home["id"]),
                        "OPPONENT_ID": str(away["id"]),
                        "HOME_AWAY": "home",
                        "GF": home_goals,
                        "GA": away_goals,
                        "WDL": wdl(home_goals, away_goals),
                    }
                )
                rows.append(
                    {
                        "SEASON": season,
                        "FIXTURE_ID": fixture_id,
                        "GAME_DATE": game_date,
                        "TEAM_ID": str(away["id"]),
                        "OPPONENT_ID": str(home["id"]),
                        "HOME_AWAY": "away",
                        "GF": away_goals,
                        "GA": home_goals,
                        "WDL": wdl(away_goals, home_goals),
                    }
                )
            await asyncio.sleep(0.3)  # polite pacing, mirrors collect_nba_data.py

    return pd.DataFrame(rows), team_codes


async def collect_lineups(fixture_ids: list[int]) -> pd.DataFrame:
    """Per-fixture lineup/appearance data (games.minutes > 0) — the football "box score",
    used ONLY by ml/training/train_football.py's historical key-player-availability backtest
    label. Never used for live Stage 2 (app/models_ml/key_player_availability.py reads only
    player_injury_status). One call per fixture — the real, unavoidable cost of this endpoint
    (confirmed live: no bulk-by-league-and-date equivalent for lineups the way /injuries has,
    see CLAUDE.md), which is why the EPL-only scope decision keeps this to ~1,900 calls rather
    than ~9,500 across all 5 leagues."""
    rows = []
    async with _football_client() as client:
        for i, fixture_id in enumerate(fixture_ids):
            response = await _get_with_retry(
                client, "/fixtures/players", {"fixture": fixture_id}
            )
            for team_block in response.json().get("response", []):
                team_id = str(team_block["team"]["id"])
                for player_row in team_block.get("players", []):
                    player = player_row["player"]
                    stats = player_row.get("statistics", [{}])[0]
                    minutes = stats.get("games", {}).get("minutes")
                    if minutes:
                        rows.append(
                            {
                                "FIXTURE_ID": fixture_id,
                                "TEAM_ID": team_id,
                                "PLAYER_NAME": player["name"],
                            }
                        )
            if (i + 1) % 100 == 0:
                print(f"  collected lineups for {i + 1}/{len(fixture_ids)} fixtures")

    return pd.DataFrame(rows, columns=["FIXTURE_ID", "TEAM_ID", "PLAYER_NAME"])


ODDS_REQUEST_DELAY_SECONDS = 5.0  # same RapidAPI-gateway-under-load caution as collect_nba_data.py
ODDS_MAX_RETRIES = 3
MAX_ODDS_DATES = 60


async def _get_odds_page(client: httpx.AsyncClient, date_str: str) -> dict | None:
    for attempt in range(ODDS_MAX_RETRIES):
        response = await client.get(
            f"/sports/{RUNDOWN_EPL_SPORT_ID}/events/{date_str}", params={"include": "scores"}
        )
        if response.status_code == 429 and attempt < ODDS_MAX_RETRIES - 1:
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else min(2**attempt * 2, 30)
            print(f"  {date_str}: 429, backing off {delay:.0f}s")
            await asyncio.sleep(delay)
            continue
        if response.status_code >= 400:
            print(f"  {date_str}: skipped ({response.status_code})")
            return None
        return response.json()
    return None


async def collect_odds_sample(game_dates: list[str]) -> pd.DataFrame:
    api_key = get_settings().therundown_api_key
    rows = []

    async with httpx.AsyncClient(
        base_url=RUNDOWN_BASE_URL,
        headers={"x-rapidapi-host": RAPIDAPI_HOST, "x-rapidapi-key": api_key},
        timeout=15.0,
    ) as client:
        for date_str in game_dates:
            payload = await _get_odds_page(client, date_str)
            await asyncio.sleep(ODDS_REQUEST_DELAY_SECONDS)
            if payload is None:
                continue
            for event in payload.get("events", []):
                for odds_payload in _map_event_to_odds_payloads(event):
                    if odds_payload.home_odds is None:
                        continue
                    rows.append(
                        {
                            "date": date_str,
                            "home_short": odds_payload.home_team_short_name,
                            "away_short": odds_payload.away_team_short_name,
                            "home_odds": odds_payload.home_odds,
                        }
                    )

    return pd.DataFrame(rows, columns=["date", "home_short", "away_short", "home_odds"])


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    games_path = DATA_DIR / "football_game_log.parquet"
    codes_path = DATA_DIR / "football_team_codes.parquet"
    if games_path.exists():
        print(f"{games_path} already exists, skipping API-Football re-fetch")
        games = pd.read_parquet(games_path)
    else:
        games, team_codes = asyncio.run(collect_game_log())
        games.to_parquet(games_path, index=False)
        print(f"saved {len(games)} game-log rows to {games_path}")
        pd.DataFrame(
            [{"team_id": k, "code": v} for k, v in team_codes.items()]
        ).to_parquet(codes_path, index=False)
        print(f"saved {len(team_codes)} team codes to {codes_path}")

    lineups_path = DATA_DIR / "football_lineups.parquet"
    if lineups_path.exists():
        print(f"{lineups_path} already exists, skipping API-Football re-fetch")
    else:
        fixture_ids = sorted(games["FIXTURE_ID"].unique().tolist())
        print(f"collecting lineups for {len(fixture_ids)} fixtures (1 call each)...")
        lineups = asyncio.run(collect_lineups(fixture_ids))
        lineups.to_parquet(lineups_path, index=False)
        print(f"saved {len(lineups)} lineup-presence rows to {lineups_path}")

    odds_path = DATA_DIR / "football_odds_sample.parquet"
    if odds_path.exists():
        print(f"{odds_path} already exists, skipping TheRundown re-fetch")
    else:
        most_recent_season = SEASONS[-1]
        recent_dates = sorted(
            games.loc[games["SEASON"] == most_recent_season, "GAME_DATE"].astype(str).unique()
        )[-MAX_ODDS_DATES:]
        print(f"pulling odds for {len(recent_dates)} dates from EPL {most_recent_season}...")
        odds = asyncio.run(collect_odds_sample(recent_dates))
        odds.to_parquet(odds_path, index=False)
        print(f"saved {len(odds)} usable odds rows to {odds_path}")


if __name__ == "__main__":
    main()
