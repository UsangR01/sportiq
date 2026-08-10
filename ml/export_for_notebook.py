"""Exports everything a standalone notebook needs to rebuild the models outside this project.

The training scripts read some inputs from Postgres (team_key_players) and the rest from the
parquet cache. A notebook running elsewhere has neither the database nor the app package, so
this copies the parquet files and materialises the DB-backed tables alongside them.

    backend/.venv/Scripts/python ml/export_for_notebook.py

Output: ml/export/  — parquet data + a README describing every file.
"""

import io
import shutil
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND))
load_dotenv(BACKEND / ".env")

DATA = REPO / "ml" / "data"
OUT = REPO / "ml" / "export"

# Per-league parquet families the football pipeline reads.
LEAGUES = ["epl", "brasileirao", "mls", "csl", "scottish_prem"]
FOOTBALL_KINDS = ["game_log", "lineups", "corners", "xg", "team_codes", "odds_sample"]

OTHER = [
    "nba_game_log.parquet",
    "nba_player_game_log.parquet",
    "nba_odds_sample.parquet",
    "tennis_game_log_atp.parquet",
    "tennis_rank_points_atp.parquet",
]


def export_key_players() -> pd.DataFrame:
    """team_key_players lives only in Postgres — Stage 1 of the Big3/Top5 feature.

    Without it the key-player features are all None in the notebook, which changes the model
    rather than merely simplifying it, so it is materialised rather than skipped.

    Server-side COPY (not psql's \copy meta-command, which does not survive a subprocess
    call) read through `docker exec` rather than a host asyncpg connection: the host->container
    path proved intermittently unreliable on this machine (ConnectionResetError WinError 64 on
    the SSL upgrade) while the container itself stayed healthy. Going through the container
    sidesteps that entirely and needs no credentials in this file."""
    import subprocess

    # No quoted column aliases in the SQL: embedded double quotes do not survive Windows
    # subprocess argument escaping, which silently yields an empty result. Renamed in pandas.
    query = (
        "select t.external_id, k.season_year, k.player_name, "
        "k.rank_metric, k.combined_metric "
        "from team_key_players k join teams t on t.id = k.team_id"
    )
    out = subprocess.run(
        [
            "docker",
            "exec",
            "sportiq-postgres-1",
            "psql",
            "-U",
            "sportiq_user",
            "-d",
            "sportiq",
            "-c",
            f"COPY ({query}) TO STDOUT WITH CSV HEADER",
        ],
        capture_output=True,
        text=True,
        # Explicit UTF-8: text=True otherwise decodes with the locale codec, which is cp1252
        # on Windows and dies on the accented player names ("Gabriel Magalhaes" and many
        # others). subprocess swallows that into a None stdout rather than raising here.
        encoding="utf-8",
        timeout=120,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"key-player export failed: {out.stderr[:300] or 'empty result'}")
    frame = pd.read_csv(io.StringIO(out.stdout))
    return frame.rename(
        columns={
            "external_id": "TEAM_ID",
            "season_year": "SEASON",
            "player_name": "PLAYER_NAME",
            "rank_metric": "RANK_METRIC",
            "combined_metric": "COMBINED_METRIC",
        }
    )


README = """# SportIQ — exported training data

Everything needed to rebuild the football, basketball and tennis models outside the project.
Open `SportIQ_Models.ipynb` in the same folder.

## Football  (8,718 training examples once assembled)

| File | Shape | What |
|---|---|---|
| `football_game_log_<league>.parquet` | 2 rows per fixture | SEASON, FIXTURE_ID, GAME_DATE, TEAM_ID, OPPONENT_ID, HOME_AWAY, GF, GA, WDL |
| `football_xg_<league>.parquet` | 2 rows per fixture | FIXTURE_ID, TEAM_ID, XG_FOR — real expected goals (TheStatsAPI) |
| `football_corners_<league>.parquet` | 2 rows per fixture | FIXTURE_ID, TEAM_ID, CORNERS |
| `football_lineups_<league>.parquet` | 1 row per appearance | FIXTURE_ID, TEAM_ID, PLAYER_NAME — who actually played |
| `football_team_codes_<league>.parquet` | 1 row per team | TEAM_ID → short code, for cross-provider odds matching |
| `football_odds_sample_epl.parquet` | sparse | Real bookmaker prices. EPL only, ~0.7% of fixtures |

Leagues: epl, brasileirao, mls, csl, scottish_prem.

xG coverage starts at different seasons per league — EPL 2022, MLS/CSL 2023, Scottish Prem
25/26 only. Earlier seasons genuinely have none upstream; they merge as NaN, which XGBoost
treats as missing.

## Basketball (NBA)

| File | Shape | What |
|---|---|---|
| `nba_game_log.parquet` | ~14,500 rows | 6 seasons of team game logs (nba_api) |
| `nba_player_game_log.parquet` | ~154,000 rows | Player box scores — used for key-player labels |
| `nba_odds_sample.parquet` | sparse | Bounded real odds sample |

## Tennis (ATP)

| File | Shape | What |
|---|---|---|
| `tennis_game_log_atp.parquet` | 35,482 rows | One row per player per completed match, 2021-2025 |
| `tennis_rank_points_atp.parquet` | 17,008 rows | Point-in-time ranking points by player and week |

## Shared

| File | What |
|---|---|
| `team_key_players.parquet` | Stage 1 of the Big3/Top5 feature — each team's top players per season, by rank metric. Exported from Postgres. |

## Two things to know before trusting a rerun

**Leakage.** Every rolling statistic must be filtered to `GAME_DATE < fixture date`. The
notebook does this, but if you modify the feature code, that strict inequality is the guard.

**Key players have two different definitions on purpose.** The training LABEL uses box-score
presence (who actually played — knowable only afterwards). Live serving uses injury status
(knowable beforehand). They must never share code; the notebook uses the training-label form,
which is correct for backtesting and wrong for forecasting.
"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    copied = []

    for league in LEAGUES:
        for kind in FOOTBALL_KINDS:
            src = DATA / f"football_{kind}_{league}.parquet"
            if src.exists():
                shutil.copy2(src, OUT / src.name)
                copied.append(src.name)

    for name in OTHER:
        src = DATA / name
        if src.exists():
            shutil.copy2(src, OUT / name)
            copied.append(name)

    key_players = export_key_players()
    key_players.to_parquet(OUT / "team_key_players.parquet", index=False)
    copied.append(f"team_key_players.parquet ({len(key_players)} rows, from Postgres)")

    (OUT / "README.md").write_text(README, encoding="utf-8")

    total_mb = sum(f.stat().st_size for f in OUT.glob("*.parquet")) / 1_048_576
    print(f"exported {len(copied)} files to {OUT}  ({total_mb:.1f} MB)")
    for name in copied:
        print(f"  {name}")


if __name__ == "__main__":
    main()
