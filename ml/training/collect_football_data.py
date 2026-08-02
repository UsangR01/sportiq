"""One-off historical data collection for football model training (TDD §3.2/§3.5).

Originally EPL-only (see CLAUDE.md's first scope decision); now also collects Brasileirão
real multi-season history so retrodicted predictions for completed Brasileirão fixtures can
draw on genuine historical form/Elo/streaks instead of only our live DB's thin (7-day) recent
window — see app/workers/backfill_predictions.py and CLAUDE.md for why this mattered enough to
justify the extra ~1,900 API calls per league.

Mirrors ml/training/collect_nba_data.py's structure: fetches real fixtures/results (this
script's own game log, not nba_api — API-Football is football's fixtures/stats source),
real per-fixture lineups (the "box score" for the historical key-player-availability backtest
label — used ONLY for that label, never live, same leakage-guard separation as NBA), and a
bounded real TheRundown odds sample (EPL only — TheRundown has zero Brazil-league coverage,
confirmed live; Brasileirão's own real API-Football odds coverage doesn't extend to historical
dates on this endpoint, so historical moneyline stays None for Brasileirão training examples,
same honest "genuinely sparse" pattern already documented for NBA).

Caches to local parquet under ml/data/, one file set per league, so re-running train_football.py
doesn't re-hit either API.

Usage (from repo root):
    backend/.venv/Scripts/python ml/training/collect_football_data.py
"""

import argparse
import asyncio
import sys
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

# Same 5-season window ml/training/compute_football_key_players.py uses for EPL. Brasileirão
# only needs its own real seasons collected here too — Stage 1 key players for it were already
# computed separately (current season only, see compute_football_key_players.py); this is
# about the MATCH RESULTS history (Elo/streaks/rolling-form/H2H), a different concern.
SEASONS = [2021, 2022, 2023, 2024, 2025]

# rundown_sport_id is None where TheRundown has no real coverage (Brasileirão) — historical
# odds collection is simply skipped for that league, not faked.
LEAGUE_CONFIGS: dict[str, dict] = {
    "epl": {"league_id": LEAGUE_IDS["epl"], "rundown_sport_id": 11},
    "brasileirao": {"league_id": LEAGUE_IDS["brasileirao"], "rundown_sport_id": None},
    # Added to fix a real, MEASURED out-of-distribution problem, not speculatively: these three
    # leagues are served by the EPL/Brasileirão-trained model, and their Over/Under-goals
    # probabilities are measurably overconfident in exactly the way that implies — mean
    # predicted P(under 3.5) vs actual was +0.02 for Brasileirão (in the training data) but
    # +0.12 for MLS and +0.08 for CSL (not in it). Collecting their real scoring history is the
    # root-cause fix; per-league calibration alone would only paper over it.
    # rundown_sport_id is None where TheRundown genuinely has no coverage (confirmed live).
    "mls": {"league_id": LEAGUE_IDS["mls"], "rundown_sport_id": 10},
    "csl": {"league_id": LEAGUE_IDS["csl"], "rundown_sport_id": None},
    "scottish_prem": {"league_id": LEAGUE_IDS["scottish_prem"], "rundown_sport_id": None},
}

# Collection is stageable because the per-fixture endpoints genuinely can't all run in one
# sitting: game logs cost ~1 call per league-season (trivial), but lineups and corners cost 1
# call PER FIXTURE — roughly 9,600 calls for the three leagues above, well past API-Football's
# 7,500/day ceiling. Stages let the cheap, highest-value data (the goal distributions that
# actually drive the calibration problem) land immediately, with the expensive per-fixture
# stages run separately across days.
#
# A league with a game log but no lineups still trains fine: its key-player features come
# through as None, which the model's own missing-value handling covers — strictly better than
# having no data for that league at all.
STAGES = ("gamelog", "corners", "lineups", "odds")

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


async def _fetch_team_codes(client: httpx.AsyncClient, league_id: int, season: int) -> dict[str, str]:
    """team external id (str) -> API-Football's own 3-letter `code` field — used as a
    best-effort join key against TheRundown's teams_normalized.abbreviation for the odds
    sample below. Not verified to match TheRundown's convention exactly for every club (the
    codebase's existing CLAUDE.md note about abbreviation consistency only ever confirmed
    BallDontLie vs TheRundown for NBA) — a best-effort match, not a guaranteed one; unmatched
    fixtures simply get no real odds/moneyline feature, same graceful-miss behavior as every
    other cross-provider join in this codebase."""
    response = await client.get("/teams", params={"league": league_id, "season": season})
    response.raise_for_status()
    return {
        str(row["team"]["id"]): row["team"]["code"]
        for row in response.json().get("response", [])
        if row["team"].get("code")
    }


async def collect_game_log(league_slug: str, league_id: int) -> tuple[pd.DataFrame, dict[str, str]]:
    """One row per team per completed fixture — the football analogue of
    collect_nba_data.py's leaguegamelog pull, built from API-Football's /fixtures rather than
    nba_api (football has no equivalent free/offline bulk endpoint). Also returns a
    team_id -> code mapping (see _fetch_team_codes) for the odds-matching step below."""
    rows = []
    team_codes: dict[str, str] = {}
    async with _football_client() as client:
        for season in SEASONS:
            team_codes.update(await _fetch_team_codes(client, league_id, season))
            print(f"fetching {league_slug} {season}-{season + 1} fixtures...")
            response = await _get_with_retry(
                client,
                "/fixtures",
                {"league": league_id, "season": season, "status": "FT-AET-PEN"},
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
                        "LEAGUE": league_slug,
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
                        "LEAGUE": league_slug,
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
    used by ml/training/train_football.py's historical key-player-availability backtest label
    AND (per the user's explicit go-ahead — no leakage concern for a one-off backtest on
    already-known outcomes) by app/workers/backfill_predictions.py's retrodiction path. One
    call per fixture — the real, unavoidable cost of this endpoint (confirmed live: no
    bulk-by-league-and-date equivalent for lineups the way /injuries has, see CLAUDE.md)."""
    rows = []
    async with _football_client() as client:
        for i, fixture_id in enumerate(fixture_ids):
            response = await _get_with_retry(client, "/fixtures/players", {"fixture": fixture_id})
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


async def collect_corner_stats(fixture_ids: list[int]) -> pd.DataFrame:
    """Per-fixture real corner-kick counts via /fixtures/statistics ("Corner Kicks" — confirmed
    live, see CLAUDE.md) — the training target for the new corners-Poisson-regressors
    (app/models_ml/football.py). One call per fixture, same real, unavoidable cost as
    collect_lineups (no bulk-by-league-and-date equivalent for per-fixture statistics either).
    Deliberately does NOT feed any new live-serving feature — the corners regressors reuse
    Layer 1's existing feature vector, so this data is training-target-only."""
    rows = []
    async with _football_client() as client:
        for i, fixture_id in enumerate(fixture_ids):
            response = await _get_with_retry(
                client, "/fixtures/statistics", {"fixture": fixture_id}
            )
            for team_block in response.json().get("response", []):
                team_id = str(team_block["team"]["id"])
                corners = next(
                    (
                        s["value"]
                        for s in team_block.get("statistics", [])
                        if s["type"] == "Corner Kicks"
                    ),
                    None,
                )
                if corners is not None:
                    rows.append(
                        {"FIXTURE_ID": fixture_id, "TEAM_ID": team_id, "CORNERS": int(corners)}
                    )
            if (i + 1) % 100 == 0:
                print(f"  collected corner stats for {i + 1}/{len(fixture_ids)} fixtures")

    return pd.DataFrame(rows, columns=["FIXTURE_ID", "TEAM_ID", "CORNERS"])


ODDS_REQUEST_DELAY_SECONDS = 5.0  # same RapidAPI-gateway-under-load caution as collect_nba_data.py
ODDS_MAX_RETRIES = 3
MAX_ODDS_DATES = 60


async def _get_odds_page(
    client: httpx.AsyncClient, rundown_sport_id: int, date_str: str
) -> dict | None:
    for attempt in range(ODDS_MAX_RETRIES):
        response = await client.get(
            f"/sports/{rundown_sport_id}/events/{date_str}", params={"include": "scores"}
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


async def collect_odds_sample(rundown_sport_id: int, game_dates: list[str]) -> pd.DataFrame:
    api_key = get_settings().therundown_api_key
    rows = []

    async with httpx.AsyncClient(
        base_url=RUNDOWN_BASE_URL,
        headers={"x-rapidapi-host": RAPIDAPI_HOST, "x-rapidapi-key": api_key},
        timeout=15.0,
    ) as client:
        for date_str in game_dates:
            payload = await _get_odds_page(client, rundown_sport_id, date_str)
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


def collect_league(league_slug: str, stages: tuple[str, ...] = STAGES) -> None:
    config = LEAGUE_CONFIGS[league_slug]
    league_id = config["league_id"]
    rundown_sport_id = config["rundown_sport_id"]

    games_path = DATA_DIR / f"football_game_log_{league_slug}.parquet"
    codes_path = DATA_DIR / f"football_team_codes_{league_slug}.parquet"
    if games_path.exists():
        print(f"{games_path} already exists, skipping API-Football re-fetch")
        games = pd.read_parquet(games_path)
    elif "gamelog" in stages:
        games, team_codes = asyncio.run(collect_game_log(league_slug, league_id))
        games.to_parquet(games_path, index=False)
        print(f"saved {len(games)} game-log rows to {games_path}")
        pd.DataFrame([{"team_id": k, "code": v} for k, v in team_codes.items()]).to_parquet(
            codes_path, index=False
        )
        print(f"saved {len(team_codes)} team codes to {codes_path}")
    else:
        # Every later stage needs the fixture ids the game log provides.
        print(f"{league_slug}: no game log yet and 'gamelog' not in stages — skipping league")
        return

    lineups_path = DATA_DIR / f"football_lineups_{league_slug}.parquet"
    if lineups_path.exists():
        print(f"{lineups_path} already exists, skipping API-Football re-fetch")
    elif "lineups" in stages:
        fixture_ids = sorted(games["FIXTURE_ID"].unique().tolist())
        print(f"collecting lineups for {len(fixture_ids)} {league_slug} fixtures (1 call each)...")
        lineups = asyncio.run(collect_lineups(fixture_ids))
        lineups.to_parquet(lineups_path, index=False)
        print(f"saved {len(lineups)} lineup-presence rows to {lineups_path}")

    corners_path = DATA_DIR / f"football_corners_{league_slug}.parquet"
    if corners_path.exists():
        print(f"{corners_path} already exists, skipping API-Football re-fetch")
    elif "corners" in stages:
        fixture_ids = sorted(games["FIXTURE_ID"].unique().tolist())
        print(
            f"collecting corner-kick stats for {len(fixture_ids)} {league_slug} fixtures "
            "(1 call each)..."
        )
        corners = asyncio.run(collect_corner_stats(fixture_ids))
        corners.to_parquet(corners_path, index=False)
        print(f"saved {len(corners)} corner-stat rows to {corners_path}")

    if rundown_sport_id is None:
        print(f"{league_slug}: no TheRundown coverage — skipping historical odds collection")
        return
    if "odds" not in stages:
        return

    odds_path = DATA_DIR / f"football_odds_sample_{league_slug}.parquet"
    if odds_path.exists():
        print(f"{odds_path} already exists, skipping TheRundown re-fetch")
    else:
        most_recent_season = SEASONS[-1]
        recent_dates = sorted(
            games.loc[games["SEASON"] == most_recent_season, "GAME_DATE"].astype(str).unique()
        )[-MAX_ODDS_DATES:]
        print(f"pulling odds for {len(recent_dates)} dates from {league_slug} {most_recent_season}...")
        odds = asyncio.run(collect_odds_sample(rundown_sport_id, recent_dates))
        odds.to_parquet(odds_path, index=False)
        print(f"saved {len(odds)} usable odds rows to {odds_path}")


def main() -> None:
    """Defaults to every league and every stage (unchanged behaviour). Both can be narrowed,
    which the per-fixture stages genuinely require given the daily API ceiling:

        # cheap: real goal distributions for the out-of-distribution leagues (~20 calls)
        python ml/training/collect_football_data.py --leagues mls,csl,scottish_prem \\
            --stages gamelog
        # expensive: run per-league, across days
        python ml/training/collect_football_data.py --leagues mls --stages corners
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--leagues",
        default=",".join(LEAGUE_CONFIGS),
        help=f"comma-separated; one or more of {','.join(LEAGUE_CONFIGS)}",
    )
    parser.add_argument(
        "--stages",
        default=",".join(STAGES),
        help=f"comma-separated; one or more of {','.join(STAGES)}",
    )
    args = parser.parse_args()

    leagues = [s.strip() for s in args.leagues.split(",") if s.strip()]
    stages = tuple(s.strip() for s in args.stages.split(",") if s.strip())
    unknown_leagues = [x for x in leagues if x not in LEAGUE_CONFIGS]
    unknown_stages = [x for x in stages if x not in STAGES]
    if unknown_leagues:
        parser.error(f"unknown league(s): {unknown_leagues}; known: {list(LEAGUE_CONFIGS)}")
    if unknown_stages:
        parser.error(f"unknown stage(s): {unknown_stages}; known: {list(STAGES)}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"leagues={leagues} stages={list(stages)}")
    for league_slug in leagues:
        collect_league(league_slug, stages)


if __name__ == "__main__":
    main()
