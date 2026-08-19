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
    # EFL Championship — investor/user demand for UK coverage (2026-08-18). TheRundown has
    # no EFL entry (probed), so rundown_sport_id is None; live odds come from
    # API-Football's own /odds, whose coverage flag is true for its current season.
    "championship": {"league_id": LEAGUE_IDS["championship"], "rundown_sport_id": None},
    # UEFA cups (2026-08-19). Two-legged ties and neutral finals make their home-advantage
    # profile genuinely different from a domestic league — worth having in training precisely
    # so the model sees that pattern, and per-league metrics will say whether it learned it.
    "ucl": {"league_id": LEAGUE_IDS["ucl"], "rundown_sport_id": 16},
    "uel": {"league_id": LEAGUE_IDS["uel"], "rundown_sport_id": None},
    "uecl": {"league_id": LEAGUE_IDS["uecl"], "rundown_sport_id": None},
    # The four remaining MVP-scope European leagues. Unlike the three above, these are NOT
    # being added to fix a measured out-of-distribution problem — they sit at roughly 2.5-3.2
    # goals/match against EPL's 2.93, so they are not scoring outliers the way Brasileirão
    # (2.41) was. They are added because they are seeded, ingest fixtures and odds today, and
    # are about to be served predictions by a model that has never seen them: their seasons
    # open mid-to-late August 2026, La Liga first.
    #
    # This cannot be solved by telling the model which league a fixture is from — league
    # identity features were built, measured, and REGRESSED (O/U trend z fell to -0.03), so
    # pooling means one shared prior and the leagues joining it need to fit that prior. These
    # do; that is the reason they are a safe addition and Brasileirão needed its own argument.
    #
    # All four have real TheRundown coverage, unlike most recent additions.
    "bundesliga": {"league_id": LEAGUE_IDS["bundesliga"], "rundown_sport_id": 13},
    "seriea": {"league_id": LEAGUE_IDS["seriea"], "rundown_sport_id": 15},
    "laliga": {"league_id": LEAGUE_IDS["laliga"], "rundown_sport_id": 14},
    "ligue1": {"league_id": LEAGUE_IDS["ligue1"], "rundown_sport_id": 12},
    # --- Tier-1 expansion candidates (top_30_football_leagues_for_prediction.md) -------------
    # League ids are LITERALS here, deliberately NOT added to app.adapters.api_football's
    # LEAGUE_IDS: fetch_injuries iterates every entry in that dict every 30 minutes, so adding
    # nine leagues there would silently spend quota polling competitions nothing serves yet.
    # These are collection-only until a retrain shows they earn a place.
    #
    # All nine were verified to carry REAL odds on upcoming fixtures (14 bookmakers each) --
    # checked against actual fixtures, not the coverage.odds flag, which claims True for
    # Allsvenskan while played fixtures return zero bookmakers. A-League Men is deliberately
    # absent: 0 bookmakers on upcoming fixtures and no TheRundown entry at all.
    #
    # rundown_sport_id is None for all but J1 League: TheRundown's own /sports list carries no
    # Nordic, Polish, Danish, Romanian, Czech or Austrian competition. J1 maps to JPN1 (19),
    # making it the only one of the nine with a second odds source and therefore the only one
    # that could ever support game-totals lines.
    # These nine carry an explicit `seasons` because SEASONS alone left them ending in 2025,
    # while the fixtures being predicted are in 2026. A game log that stops months before the
    # match still produces form/Elo/streak values -- just stale ones -- and stale-but-present
    # is invisible to feature_completeness, which measures presence rather than freshness.
    # Measured consequence: picks in these leagues went 11/24 against 19.7 expected (claimed
    # 82.3%, actual 45.8%, P=0.0001) while established leagues were fine at 16/19.
    "allsvenskan": {"league_id": 113, "rundown_sport_id": None, "seasons": SEASONS + [2026]},
    "eliteserien": {"league_id": 103, "rundown_sport_id": None, "seasons": SEASONS + [2026]},
    "veikkausliiga": {"league_id": 244, "rundown_sport_id": None, "seasons": SEASONS + [2026]},
    "ekstraklasa": {"league_id": 106, "rundown_sport_id": None, "seasons": SEASONS + [2026]},
    "denmark_superliga": {
        "league_id": 119,
        "rundown_sport_id": None,
        "seasons": SEASONS + [2026],
    },
    "liga_i": {"league_id": 283, "rundown_sport_id": None, "seasons": SEASONS + [2026]},
    # J1 needs BOTH: 2026 is the short transitional Feb-Jun season, and 2027 is the current
    # one running 2026-08-07 -> 2027-06-06 (API-Football labels it by the year it ENDS -- see
    # END_YEAR_SEASON_LEAGUES in app/adapters/api_football.py). Collecting only "2026" here
    # would miss every match played from August 2026 onward.
    "j1_league": {"league_id": 98, "rundown_sport_id": 19, "seasons": SEASONS + [2026, 2027]},
    "czech_first": {"league_id": 345, "rundown_sport_id": None, "seasons": SEASONS + [2026]},
    "austria_bundesliga": {
        "league_id": 218,
        "rundown_sport_id": None,
        "seasons": SEASONS + [2026],
    },
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
#
# "lineups" is now DEAD WEIGHT for the 1X2/goals model and should not be run for a newly-added
# league: the four key-player features it feeds were pruned from FEATURE_NAMES (they measured
# at ~0.000 importance and the pruned vector beat the full one), so train_football.py still
# loads lineups but the model never sees them. At 1 call per fixture this is the single most
# expensive stage, so skipping it is what makes adding a league affordable — roughly 5,600
# calls saved across the four European leagues. Left in place rather than deleted because
# removing the key-player pipeline end to end is its own tracked change.
STAGES = ("gamelog", "corners", "lineups", "odds")

LINEUP_COLUMNS = ["FIXTURE_ID", "TEAM_ID", "PLAYER_NAME"]
# Flush partial lineup progress this often. Small enough that an interrupted run loses only a
# few hundred calls, large enough not to rewrite the parquet constantly.
LINEUP_CHECKPOINT_EVERY = 200

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


async def _fetch_teams(
    client: httpx.AsyncClient, league_id: int, season: int
) -> tuple[dict[str, str], dict[str, str]]:
    """Returns (codes, names), both keyed by team external id (str), from ONE /teams call.

    `code` is API-Football's own 3-letter field — a best-effort join key against TheRundown's
    teams_normalized.abbreviation for the odds sample below. Not verified to match TheRundown's
    convention exactly for every club (the codebase's existing CLAUDE.md note about
    abbreviation consistency only ever confirmed BallDontLie vs TheRundown for NBA) — unmatched
    fixtures simply get no real odds/moneyline feature, the same graceful-miss behavior as
    every other cross-provider join here.

    `name` is captured because collect_thestatsapi_xg.py's resolve step needs it as the
    tiebreak when several fixtures share a (date, home goals, away goals) key — measured at
    15-23% of fixtures per league, so it is not a rare edge case. That step previously read
    names from the DB alone, which silently yields NOTHING for a league whose fixtures have
    never been ingested (all four European leagues added here have zero Team rows: their
    seasons have not started). With no names the similarity score is 0.0, every ambiguous
    fixture trips the too_weak guard, and roughly a fifth of real xG is dropped. Same call,
    same quota — the response already carried this field.
    """
    response = await client.get("/teams", params={"league": league_id, "season": season})
    response.raise_for_status()
    codes: dict[str, str] = {}
    names: dict[str, str] = {}
    for row in response.json().get("response", []):
        team = row["team"]
        team_id = str(team["id"])
        if team.get("code"):
            codes[team_id] = team["code"]
        if team.get("name"):
            names[team_id] = team["name"]
    return codes, names


async def collect_game_log(
    league_slug: str, league_id: int, seasons: list[int] | None = None
) -> tuple[pd.DataFrame, dict[str, str], dict[str, str]]:
    """One row per team per completed fixture — the football analogue of
    collect_nba_data.py's leaguegamelog pull, built from API-Football's /fixtures rather than
    nba_api (football has no equivalent free/offline bulk endpoint). Also returns team_id ->
    code and team_id -> name mappings (see _fetch_teams) for the odds-matching step below and
    for xG resolution respectively."""
    rows = []
    team_codes: dict[str, str] = {}
    team_names: dict[str, str] = {}
    async with _football_client() as client:
        for season in seasons if seasons is not None else SEASONS:
            season_codes, season_names = await _fetch_teams(client, league_id, season)
            team_codes.update(season_codes)
            team_names.update(season_names)
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

    return pd.DataFrame(rows), team_codes, team_names


async def collect_lineups(
    fixture_ids: list[int], checkpoint_path: Path | None = None
) -> pd.DataFrame:
    """Per-fixture lineup/appearance data (games.minutes > 0) — the football "box score",
    used by ml/training/train_football.py's historical key-player-availability backtest label
    AND (per the user's explicit go-ahead — no leakage concern for a one-off backtest on
    already-known outcomes) by app/workers/backfill_predictions.py's retrodiction path. One
    call per fixture — the real, unavoidable cost of this endpoint (confirmed live: no
    bulk-by-league-and-date equivalent for lineups the way /injuries has, see CLAUDE.md).

    Checkpointed, because one call per fixture across three leagues is ~4,900 calls against a
    7,500/day ceiling — the work genuinely spans days. Without this, exhausting the quota at
    99% of a league discarded every call made, which is exactly the all-or-nothing failure that
    cost a ~7-hour tennis rank-points run earlier (see collect_tennis_data.py). Progress is
    flushed every LINEUP_CHECKPOINT_EVERY fixtures and already-collected fixtures are skipped
    on resume, so an interrupted run costs at most that many calls."""
    rows: list[dict] = []
    done: set = set()
    if checkpoint_path is not None and checkpoint_path.exists():
        cached = pd.read_parquet(checkpoint_path)
        rows = cached.to_dict("records")
        done = set(cached["FIXTURE_ID"].tolist())
        fixture_ids = [f for f in fixture_ids if f not in done]
        print(f"  resuming: {len(done)} fixtures already collected, {len(fixture_ids)} remaining")

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
            if checkpoint_path is not None and (i + 1) % LINEUP_CHECKPOINT_EVERY == 0:
                pd.DataFrame(rows, columns=LINEUP_COLUMNS).to_parquet(checkpoint_path, index=False)

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


def _merge_lookup(path: Path, fresh: dict[str, str], value_column: str) -> None:
    """Merge newly-seen team_id -> code/name pairs into an existing lookup parquet.

    New seasons bring promoted clubs, so these files have to grow alongside the game log. A
    missing NAME is not cosmetic: it is what stops roughly 20% of real xG being dropped as
    ambiguous during resolution (see _fetch_teams)."""
    if not fresh:
        return
    rows = pd.DataFrame([{"team_id": k, value_column: v} for k, v in fresh.items()])
    if path.exists():
        rows = pd.concat([pd.read_parquet(path), rows], ignore_index=True).drop_duplicates(
            subset=["team_id"], keep="last"
        )
    rows.to_parquet(path, index=False)
    print(f"  lookup {path.name} now {len(rows)} rows")


def collect_league(
    league_slug: str, stages: tuple[str, ...] = STAGES, retry_missing_corners: bool = False
) -> None:
    config = LEAGUE_CONFIGS[league_slug]
    league_id = config["league_id"]
    rundown_sport_id = config["rundown_sport_id"]

    games_path = DATA_DIR / f"football_game_log_{league_slug}.parquet"
    codes_path = DATA_DIR / f"football_team_codes_{league_slug}.parquet"
    names_path = DATA_DIR / f"football_team_names_{league_slug}.parquet"
    wanted = sorted(config.get("seasons", SEASONS))
    # A MISSING TEAM-NAMES FILE MUST NOT SKIP THE WHOLE LEAGUE. Requiring names here was
    # deliberate for the gamelog stage — a log collected before names were captured should
    # re-run, since names are what stop ~20% of real xG being dropped as ambiguous (see
    # _fetch_teams). But it was applied to the league as a whole, so with any OTHER stage
    # selected the league fell through to the `else` below and returned immediately.
    #
    # Silent, and it cost a real backfill: brasileirao, csl, epl and scottish_prem are exactly
    # the four leagues with no team_names parquet, and exactly the four that gained ZERO from an
    # 8,500-call corners backfill that lifted every other league from ~66% to ~92%. The log said
    # "no game log yet" about files holding 3,800 rows. Corners need only the fixture ids, which
    # the game log has always had.
    needs_name_refetch = not names_path.exists()
    if games_path.exists() and not (needs_name_refetch and "gamelog" in stages):
        games = pd.read_parquet(games_path)
        have = set(games["SEASON"].astype(int).tolist()) if "SEASON" in games else set()
        missing = [s for s in wanted if s not in have]
        if missing and "gamelog" in stages:
            # ADDITIVE, not all-or-nothing. This used to skip outright whenever the parquet
            # existed, which quietly made the script a one-off: a league collected through 2025
            # stayed frozen there, and its retrodictions kept deriving form/Elo from a game log
            # ending months before the fixture being predicted. Stale-but-present features look
            # exactly like good ones -- feature_completeness measures presence, not freshness --
            # so nothing downstream could notice. Fetching only the missing seasons costs ~1
            # call per league-season and makes this re-runnable every season.
            print(f"{league_slug}: have seasons {sorted(have)}, fetching missing {missing}")
            fresh, fresh_codes, fresh_names = asyncio.run(
                collect_game_log(league_slug, league_id, seasons=missing)
            )
            if not fresh.empty:
                games = pd.concat([games, fresh], ignore_index=True).drop_duplicates(
                    subset=["FIXTURE_ID", "TEAM_ID"], keep="last"
                )
                games.to_parquet(games_path, index=False)
                print(f"merged {len(fresh)} new rows; game log now {len(games)} rows")
                _merge_lookup(codes_path, fresh_codes, "code")
                _merge_lookup(names_path, fresh_names, "name")
        else:
            print(f"{games_path} covers seasons {wanted}, skipping API-Football re-fetch")
    elif "gamelog" in stages:
        # A game log collected before team names were captured re-runs here deliberately: the
        # names are what stop ~20% of real xG being dropped as ambiguous (see _fetch_teams),
        # and re-fetching is only ~1 call per league-season.
        games, team_codes, team_names = asyncio.run(
            collect_game_log(league_slug, league_id, seasons=wanted)
        )
        games.to_parquet(games_path, index=False)
        print(f"saved {len(games)} game-log rows to {games_path}")
        pd.DataFrame([{"team_id": k, "code": v} for k, v in team_codes.items()]).to_parquet(
            codes_path, index=False
        )
        print(f"saved {len(team_codes)} team codes to {codes_path}")
        pd.DataFrame([{"team_id": k, "name": v} for k, v in team_names.items()]).to_parquet(
            names_path, index=False
        )
        print(f"saved {len(team_names)} team names to {names_path}")
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
        checkpoint_path = DATA_DIR / f"football_lineups_{league_slug}.checkpoint.parquet"
        lineups = asyncio.run(collect_lineups(fixture_ids, checkpoint_path))
        lineups.to_parquet(lineups_path, index=False)
        # Only once the real file is written — the checkpoint is the resume point until then.
        checkpoint_path.unlink(missing_ok=True)
        print(f"saved {len(lineups)} lineup-presence rows to {lineups_path}")

    corners_path = DATA_DIR / f"football_corners_{league_slug}.parquet"
    if corners_path.exists() and "corners" in stages:
        # Additive, for the same reason the game-log stage is: once a league gains a new
        # season, "the parquet exists" stops meaning "the parquet is complete". Skipping
        # outright left the newest and most relevant fixtures permanently without corners.
        existing = pd.read_parquet(corners_path)
        have = set(existing["FIXTURE_ID"].astype(str))
        # Only fixtures NEWER than the newest one already covered. "Not in the parquet" is not
        # the same as "not yet attempted": /fixtures/statistics genuinely returns no corner
        # count for a real 26-45% of historical fixtures, and those misses are indistinguishable
        # from never-tried, because the parquet only records successes. Treating every absence
        # as a to-do meant 4,973 calls for these nine leagues, roughly 4,000 of them re-asking
        # for data the provider has already said it does not have. Bounding by date targets the
        # newly-collected seasons, which is the actual gap.
        covered = games[games["FIXTURE_ID"].astype(str).isin(have)]
        cutoff = pd.to_datetime(covered["GAME_DATE"]).max() if not covered.empty else None
        if retry_missing_corners:
            # OPT-IN, and the date bound above exists for a reason that has since expired. It
            # was chosen when the plan allowed 7,500 requests/day, where re-asking for every
            # absence cost ~4,973 calls for nine leagues with roughly 4,000 of them re-requesting
            # data the provider had already declined to supply. On Ultra (75,000/day, confirmed
            # live) that arithmetic no longer bites: the whole 18-league backfill is ~8,500
            # calls, about 11% of one day.
            #
            # And the absences are NOT all provider-side. Sampling 12 uncovered fixtures across
            # three leagues, 8 returned real corner counts on a straight re-request — only
            # Veikkausliiga's 2021 gaps came back genuinely empty, which matches its 35%
            # coverage being the worst by a wide margin. Those are collection misses, not
            # missing data.
            #
            # Still opt-in rather than the default: the default should stay cheap, and a run
            # that re-asks for known-absent data is only worth it when the budget is spare.
            candidates = games
        elif cutoff is None:
            candidates = games
        else:
            candidates = games[pd.to_datetime(games["GAME_DATE"]) > cutoff]
        missing = [
            fid
            for fid in sorted(candidates["FIXTURE_ID"].unique().tolist())
            if str(fid) not in have
        ]
        if cutoff is not None:
            print(f"{league_slug}: corners covered through {cutoff.date()}")
        if missing:
            print(f"collecting corners for {len(missing)} new {league_slug} fixtures...")
            fresh = asyncio.run(collect_corner_stats(missing))
            if not fresh.empty:
                combined = pd.concat([existing, fresh], ignore_index=True).drop_duplicates(
                    subset=["FIXTURE_ID", "TEAM_ID"], keep="last"
                )
                combined.to_parquet(corners_path, index=False)
                print(f"merged {len(fresh)} rows; corners now {len(combined)}")
        else:
            print(f"{corners_path} already covers every fixture in the game log")
    elif corners_path.exists():
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
        print(
            f"pulling odds for {len(recent_dates)} dates from {league_slug} {most_recent_season}..."
        )
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
    parser.add_argument(
        "--retry-missing-corners",
        action="store_true",
        help=(
            "re-request /fixtures/statistics for EVERY fixture with no corners row, not just "
            "those newer than the last covered date. ~8,500 calls across all 18 leagues; only "
            "worth it with spare daily budget (see the comment in collect_league)"
        ),
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
        collect_league(league_slug, stages, retry_missing_corners=args.retry_missing_corners)


if __name__ == "__main__":
    main()
