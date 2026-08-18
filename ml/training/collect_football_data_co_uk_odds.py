"""Pull historical odds from football-data.co.uk into ml/data/ — the free 17-of-18-league source.

WHY THIS SOURCE. Training features must exist for HISTORICAL fixtures, and API-Football's odds
do not extend backwards (confirmed live for Brasileirão long ago). football-data.co.uk publishes
free CSVs with real bookmaker odds going back decades. Researched 2026-08-18 against the actual
file headers, not the marketing page — the coverage splits sharply in two:

  MAIN files (one per league-season, mmz4281/<yy1yy2>/<code>.csv):
    our epl, scottish_prem, bundesliga, seriea, laliga, ligue1
    1X2 from ~7 books, Over/Under 2.5 (Bet365 + Pinnacle + market max/avg), Asian handicap —
    each in OPENING and CLOSING (C-suffixed) form — plus shots/corners/referee columns.

  EXTRA files (one per country covering all seasons, new/<code>.csv):
    our brasileirao, mls, csl, j1_league, allsvenskan, eliteserien, veikkausliiga,
    denmark_superliga, ekstraklasa, liga_i, austria_bundesliga
    CLOSING 1X2 ONLY (Pinnacle, Bet365, Betfair Exchange, market max/avg). No Over/Under.

  czech_first is not offered at all — the one real gap of our 18.

Season labels: main files use the start-year pair ("2122"); extra files carry a Season column
(either "2021" for calendar leagues or "2021/2022" for European ones). Both are normalised here
to OUR convention — the season START year as an int, matching the game-log parquets — so a
training join needs no per-league relabelling.

Raw CSVs are cached under ml/data/football_data_co_uk/ and only re-downloaded with --refresh
(the current European season and every extra file are still being appended to upstream, so a
refresh shortly before a training run is the intended workflow). The normalised output is ONE
parquet, ml/data/odds_history_football_data_co_uk.parquet. Deliberately NOT shipped in the
Docker image: odds history is a training-time input, nothing serves from it.

    backend/.venv/Scripts/python ml/training/collect_football_data_co_uk_odds.py
    backend/.venv/Scripts/python ml/training/collect_football_data_co_uk_odds.py --refresh
"""

import argparse
import time
from io import StringIO
from pathlib import Path

import httpx
import pandas as pd

ML_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = ML_DIR / "data" / "football_data_co_uk"
OUT_PATH = ML_DIR / "data" / "odds_history_football_data_co_uk.parquet"

BASE = "https://www.football-data.co.uk"
REQUEST_DELAY_SECONDS = 0.6  # polite pacing for a free host; ~41 small files total

# Our season windows: 2021..2025 completed + the in-progress 2026 season where it exists.
MAIN_SEASON_CODES = ["2122", "2223", "2324", "2425", "2526", "2627"]

MAIN_LEAGUES = {
    "epl": "E0",
    "scottish_prem": "SC0",
    "bundesliga": "D1",
    "seriea": "I1",
    "laliga": "SP1",
    "ligue1": "F1",
}

EXTRA_LEAGUES = {
    "brasileirao": "BRA",
    "mls": "USA",
    "csl": "CHN",
    "j1_league": "JPN",
    "allsvenskan": "SWE",
    "eliteserien": "NOR",
    "veikkausliiga": "FIN",
    "denmark_superliga": "DNK",  # not DEN — probed, DEN is a 404
    "ekstraklasa": "POL",
    "liga_i": "ROU",
    "austria_bundesliga": "AUT",
}

# Normalised name -> candidate source columns, first present wins. Opening odds and O/U exist
# only in main files; extra-file rows get NaN there, which is the honest representation.
ODDS_COLUMNS = {
    # closing 1X2 (both file kinds)
    "close_pinnacle_home": ["PSCH"],
    "close_pinnacle_draw": ["PSCD"],
    "close_pinnacle_away": ["PSCA"],
    "close_b365_home": ["B365CH"],
    "close_b365_draw": ["B365CD"],
    "close_b365_away": ["B365CA"],
    "close_avg_home": ["AvgCH"],
    "close_avg_draw": ["AvgCD"],
    "close_avg_away": ["AvgCA"],
    "close_max_home": ["MaxCH"],
    "close_max_draw": ["MaxCD"],
    "close_max_away": ["MaxCA"],
    # opening 1X2 (main files only)
    "open_pinnacle_home": ["PSH"],
    "open_pinnacle_draw": ["PSD"],
    "open_pinnacle_away": ["PSA"],
    "open_b365_home": ["B365H"],
    "open_b365_draw": ["B365D"],
    "open_b365_away": ["B365A"],
    "open_avg_home": ["AvgH"],
    "open_avg_draw": ["AvgD"],
    "open_avg_away": ["AvgA"],
    # Over/Under 2.5 goals (main files only)
    "open_b365_over25": ["B365>2.5"],
    "open_b365_under25": ["B365<2.5"],
    "open_pinnacle_over25": ["P>2.5"],
    "open_pinnacle_under25": ["P<2.5"],
    "open_avg_over25": ["Avg>2.5"],
    "open_avg_under25": ["Avg<2.5"],
    "close_b365_over25": ["B365C>2.5"],
    "close_b365_under25": ["B365C<2.5"],
    "close_pinnacle_over25": ["PC>2.5"],
    "close_pinnacle_under25": ["PC<2.5"],
    "close_avg_over25": ["AvgC>2.5"],
    "close_avg_under25": ["AvgC<2.5"],
}


def _download(client: httpx.Client, url_path: str, dest: Path, refresh: bool) -> bool:
    if dest.exists() and not refresh:
        return True
    response = client.get(f"{BASE}/{url_path}")
    time.sleep(REQUEST_DELAY_SECONDS)
    if response.status_code != 200 or not response.text.strip():
        print(f"  MISSING upstream: {url_path} (HTTP {response.status_code})")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(response.text, encoding="utf-8")
    return True


def _read_csv(path: Path) -> pd.DataFrame:
    # utf-8-sig strips the BOM the site ships; bad lines occur in a few older files where a
    # trailing comma count drifts — skipping them loses the row, never corrupts a column.
    return pd.read_csv(
        StringIO(path.read_text(encoding="utf-8-sig")),
        on_bad_lines="skip",
    )


def _season_start_year_main(code: str) -> int:
    return 2000 + int(code[:2])


def _season_start_year_extra(raw: object) -> int | None:
    """'2021' (calendar league) -> 2021; '2021/2022' (European) -> 2021."""
    text = str(raw).strip()
    if not text or text.lower() == "nan":
        return None
    return int(text.split("/")[0])


def _normalise(frame: pd.DataFrame, league: str, season: int | None) -> pd.DataFrame:
    home_col = "HomeTeam" if "HomeTeam" in frame.columns else "Home"
    away_col = "AwayTeam" if "AwayTeam" in frame.columns else "Away"
    goals_home = "FTHG" if "FTHG" in frame.columns else "HG"
    goals_away = "FTAG" if "FTAG" in frame.columns else "AG"
    out = pd.DataFrame(
        {
            "league": league,
            "date": pd.to_datetime(frame["Date"], dayfirst=True, errors="coerce"),
            "home_name": frame[home_col].astype(str).str.strip(),
            "away_name": frame[away_col].astype(str).str.strip(),
            "home_goals": pd.to_numeric(frame[goals_home], errors="coerce"),
            "away_goals": pd.to_numeric(frame[goals_away], errors="coerce"),
        }
    )
    if season is not None:
        out["season"] = season
    else:
        out["season"] = frame["Season"].map(_season_start_year_extra)
    for name, sources in ODDS_COLUMNS.items():
        col = next((c for c in sources if c in frame.columns), None)
        out[name] = pd.to_numeric(frame[col], errors="coerce") if col else pd.NA
    # A row with no parseable date or no completed score is a fixture list artefact, not a match.
    return out.dropna(subset=["date", "home_goals", "away_goals"])


def main(refresh: bool) -> None:
    frames: list[pd.DataFrame] = []
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for league, code in MAIN_LEAGUES.items():
            for season_code in MAIN_SEASON_CODES:
                dest = RAW_DIR / f"{code}_{season_code}.csv"
                if not _download(client, f"mmz4281/{season_code}/{code}.csv", dest, refresh):
                    continue
                frame = _read_csv(dest)
                if frame.empty:
                    continue
                frames.append(_normalise(frame, league, _season_start_year_main(season_code)))
        for league, code in EXTRA_LEAGUES.items():
            dest = RAW_DIR / f"{code}.csv"
            if not _download(client, f"new/{code}.csv", dest, refresh):
                continue
            frame = _read_csv(dest)
            # Extra files bundle every season since ~2012; our game logs start at 2021.
            normalised = _normalise(frame, league, None)
            frames.append(normalised[normalised["season"] >= 2021])

    combined = pd.concat(frames, ignore_index=True)
    combined.to_parquet(OUT_PATH, index=False)
    print(f"\nwrote {len(combined)} rows to {OUT_PATH}\n")

    coverage = (
        combined.assign(
            has_close_1x2=combined["close_avg_home"].notna()
            | combined["close_pinnacle_home"].notna(),
            has_ou25=combined["close_avg_over25"].notna() | combined["open_avg_over25"].notna(),
        )
        .groupby("league")
        .agg(
            rows=("league", "size"),
            seasons=("season", lambda s: f"{int(s.min())}-{int(s.max())}"),
            close_1x2=("has_close_1x2", "mean"),
            ou25=("has_ou25", "mean"),
        )
        .sort_index()
    )
    print(coverage.to_string(float_format=lambda x: f"{x:.0%}"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="re-download cached raw CSVs")
    main(parser.parse_args().refresh)
