"""One-off historical data collection for NBA model training (TDD §3.5's "historical
training data" step, TDD §7's nba_api endpoints table). Run locally, not from a cloud
worker — this repeats the same nba_api cloud-IP-block constraint as TDD §7/§2.1 describes,
even though this particular script just happens to already be run from a non-cloud machine.

Caches to local parquet under ml/data/ so re-running train_nba.py doesn't re-hit either API.

Usage (from repo root):
    PYTHONPATH=backend python ml/training/collect_nba_data.py
"""

import asyncio
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# pydantic-settings resolves `env_file=".env"` against the process's CURRENT WORKING
# DIRECTORY, not this file's location or sys.path — running this script from the repo root
# (rather than from backend/) silently reads a blank .env and every setting falls back to
# its default (e.g. therundown_api_key=""), which manifests downstream as a confusing 401
# that looks like a rate-limit/account problem but isn't. Loading explicitly by absolute
# path here makes this script's behaviour independent of the caller's cwd.
load_dotenv(BACKEND_DIR / ".env")

import httpx  # noqa: E402
import pandas as pd  # noqa: E402
from app.adapters.therundown import (  # noqa: E402
    BASE_URL,
    RAPIDAPI_HOST,
    _map_event_to_odds_payloads,
)
from app.core.config import get_settings  # noqa: E402
from nba_api.stats.endpoints import leaguegamelog, playergamelogs  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 6 seasons (TDD §3.5 asks for "3+ seasons minimum", we have room for more since nba_api is
# fast and unthrottled from here) — 4 for training, 1 for validation, 1 (the most recently
# completed) held out as the test season.
SEASONS = ["2020-21", "2021-22", "2022-23", "2023-24", "2024-25", "2025-26"]

# Historical odds coverage on TheRundown's current subscription is sparse and inconsistent
# (confirmed live — see CLAUDE.md), and it's a rate-limited, recently-subscribed resource.
# Bounded to the most recent 60 game-dates of the most recent season rather than the full
# ~170 dates across 6 seasons, to keep this a deliberately small, controlled pull.
MAX_ODDS_DATES = 60
RUNDOWN_NBA_SPORT_ID = 4


def collect_game_log() -> pd.DataFrame:
    frames = []
    for season in SEASONS:
        print(f"fetching {season} game log...")
        result = leaguegamelog.LeagueGameLog(
            season=season, season_type_all_star="Regular Season", timeout=30
        )
        df = result.get_data_frames()[0]
        df["SEASON"] = season
        frames.append(df)
        time.sleep(0.3)  # polite pacing even though no rate limit has been observed locally

    games = pd.concat(frames, ignore_index=True)
    games["GAME_DATE"] = pd.to_datetime(games["GAME_DATE"]).dt.date
    return games


def collect_player_game_log() -> pd.DataFrame:
    """Bulk per-season player-level game log (one call/season, like collect_game_log) — used
    ONLY by ml/training/train_nba.py's historical box-score-presence backtest label for the
    key-player-availability feature. Never used for live Stage 2 (app/models_ml/
    nba_key_players.py:get_key_player_availability reads only player_injury_status)."""
    frames = []
    for season in SEASONS:
        print(f"fetching {season} player game log...")
        result = playergamelogs.PlayerGameLogs(
            season_nullable=season, season_type_nullable="Regular Season", timeout=30
        )
        df = result.get_data_frames()[0]
        df["SEASON"] = season
        frames.append(df)
        time.sleep(0.3)

    logs = pd.concat(frames, ignore_index=True)
    logs["GAME_DATE"] = pd.to_datetime(logs["GAME_DATE"]).dt.date
    return logs


ODDS_REQUEST_DELAY_SECONDS = 5.0  # a first attempt at 60 back-to-back requests with no delay
# triggered a sustained run of 429s, and even a 1.5s gap wasn't enough — the 429s escalated
# into 401s on retry (a RapidAPI-gateway quirk under sustained load, not a real auth
# failure: manually confirmed the key still works seconds later at normal pace). 5s between
# requests was confirmed live to be reliably under the threshold. This isn't the adapter's
# own _get_with_retry (that lives in app.adapters; this script is a one-off outside it), so
# the same retry-with-backoff idea is reimplemented minimally here.
ODDS_MAX_RETRIES = 3


async def _get_odds_page(client: httpx.AsyncClient, date_str: str) -> dict | None:
    for attempt in range(ODDS_MAX_RETRIES):
        response = await client.get(
            f"/sports/{RUNDOWN_NBA_SPORT_ID}/events/{date_str}", params={"include": "scores"}
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
    """Bounded historical odds pull for assemble_from_game_log's moneyline_implied_prob_home
    feature. Expect mostly-empty results — that's the confirmed real behaviour of this
    provider's historical coverage, not a bug in this script."""
    api_key = get_settings().therundown_api_key
    rows = []

    async with httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"x-rapidapi-host": RAPIDAPI_HOST, "x-rapidapi-key": api_key},
        timeout=15.0,
    ) as client:
        for date_str in game_dates:
            payload = await _get_odds_page(client, date_str)
            await asyncio.sleep(ODDS_REQUEST_DELAY_SECONDS)
            if payload is None:
                continue

            for event in payload.get("events", []):
                for payload in _map_event_to_odds_payloads(event):
                    if payload.home_odds is None:
                        continue
                    rows.append(
                        {
                            "date": date_str,
                            "home_short": payload.home_team_short_name,
                            "away_short": payload.away_team_short_name,
                            "home_odds": payload.home_odds,
                        }
                    )

    return pd.DataFrame(rows, columns=["date", "home_short", "away_short", "home_odds"])


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    games_path = DATA_DIR / "nba_game_log.parquet"
    if games_path.exists():
        print(f"{games_path} already exists, skipping nba_api re-fetch")
        games = pd.read_parquet(games_path)
    else:
        games = collect_game_log()
        games.to_parquet(games_path, index=False)
        print(f"saved {len(games)} game-log rows to {games_path}")

    player_log_path = DATA_DIR / "nba_player_game_log.parquet"
    if player_log_path.exists():
        print(f"{player_log_path} already exists, skipping nba_api re-fetch")
    else:
        player_log = collect_player_game_log()
        player_log.to_parquet(player_log_path, index=False)
        print(f"saved {len(player_log)} player-game-log rows to {player_log_path}")

    odds_path = DATA_DIR / "nba_odds_sample.parquet"
    if odds_path.exists():
        print(f"{odds_path} already exists, skipping TheRundown re-fetch")
    else:
        most_recent_season = SEASONS[-1]
        recent_dates = sorted(
            games.loc[games["SEASON"] == most_recent_season, "GAME_DATE"].astype(str).unique()
        )[-MAX_ODDS_DATES:]
        print(f"pulling odds for {len(recent_dates)} dates from {most_recent_season}...")

        odds = asyncio.run(collect_odds_sample(recent_dates))
        odds.to_parquet(odds_path, index=False)
        print(f"saved {len(odds)} usable odds rows to {odds_path}")


if __name__ == "__main__":
    main()
