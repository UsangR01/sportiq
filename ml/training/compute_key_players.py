"""Stage 1 (TDD §3.3, "Big3 / Top5 Key Player Availability Feature"): season-level Top 5 key
player identification per team. Backward-looking and leakage-safe — ranks by trailing WS/48
among a 26+ MPG pool (falling back to 18-26 MPG if fewer than 5 qualify), independent of any
single game's box score. Run once per season; safe to re-run (upserts by deleting and
re-inserting each team+season's rows).

The pure ranking/scoring logic lives in app/models_ml/nba_key_players.py (shared with Stage
2's live lookup module, though Stage 1 and Stage 2 never share DB-query logic — only the
scoring functions are common ground). This script is the thin nba_api-fetching + DB-writing
wrapper around it, matching the same pattern as collect_nba_data.py/train_nba.py.

Usage (from repo root):
    backend/.venv/Scripts/python ml/training/compute_key_players.py
"""

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
load_dotenv(BACKEND_DIR / ".env")  # see collect_nba_data.py for why this is needed explicitly

from nba_api.stats.endpoints import leaguedashplayerstats, leaguedashteamstats  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from app.core.database import async_session_factory  # noqa: E402
from app.fixtures.models import Team, TeamKeyPlayer  # noqa: E402
from app.models_ml.nba_key_players import compute_uper, compute_ws48_approx, select_top5  # noqa: E402

# Same 6 seasons as ml/training/collect_nba_data.py — Stage 1 needs its own ranking per
# season, computed independently, not just for "the current" one.
SEASONS = ["2020-21", "2021-22", "2022-23", "2023-24", "2024-25", "2025-26"]

# Restricts the league-average-raw-per48 baseline (used to normalise `per` toward PER's own
# 15.0-average convention) to players with meaningful playing time, so a handful of
# garbage-time call-ups don't skew it.
MIN_MINUTES_FOR_LEAGUE_AVG = 10.0


def _season_start_year(season: str) -> int:
    return int(season.split("-")[0])


def _fetch_season_player_rows(season: str) -> list[dict]:
    base = leaguedashplayerstats.LeagueDashPlayerStats(
        season=season, measure_type_detailed_defense="Base", per_mode_detailed="PerGame", timeout=30
    ).get_data_frames()[0]
    advanced = leaguedashplayerstats.LeagueDashPlayerStats(
        season=season, measure_type_detailed_defense="Advanced", per_mode_detailed="PerGame", timeout=30
    ).get_data_frames()[0]
    teams = leaguedashteamstats.LeagueDashTeamStats(
        season=season, measure_type_detailed_defense="Base", per_mode_detailed="PerGame", timeout=30
    ).get_data_frames()[0]

    pie_by_player = dict(zip(advanced["PLAYER_ID"], advanced["PIE"], strict=False))
    # leaguedashteamstats has no TEAM_ABBREVIATION column at all (only TEAM_ID/TEAM_NAME) —
    # join team win_pct onto players by TEAM_ID, which both endpoints do have.
    win_pct_by_team_id = dict(zip(teams["TEAM_ID"], teams["W_PCT"], strict=False))

    rows = []
    for _, row in base.iterrows():
        rows.append(
            {
                "PLAYER_ID": str(row["PLAYER_ID"]),
                "PLAYER_NAME": row["PLAYER_NAME"],
                "TEAM_ABBREVIATION": row["TEAM_ABBREVIATION"],
                "MIN": row["MIN"],
                "PTS": row["PTS"],
                "REB": row["REB"],
                "AST": row["AST"],
                "STL": row["STL"],
                "BLK": row["BLK"],
                "TOV": row["TOV"],
                "PF": row["PF"],
                "FGA": row["FGA"],
                "FGM": row["FGM"],
                "FTA": row["FTA"],
                "FTM": row["FTM"],
                "PIE": pie_by_player.get(row["PLAYER_ID"]),
                "team_win_pct": win_pct_by_team_id.get(row["TEAM_ID"], 0.5),
            }
        )
    return rows


def _league_avg_raw_per48(rows: list[dict]) -> float:
    meaningful = [r for r in rows if r["MIN"] >= MIN_MINUTES_FOR_LEAGUE_AVG]
    if not meaningful:
        return 0.0
    values = []
    for r in meaningful:
        raw = (
            r["PTS"]
            + r["REB"]
            + r["AST"]
            + r["STL"]
            + r["BLK"]
            - (r["FGA"] - r["FGM"])
            - (r["FTA"] - r["FTM"])
            - r["TOV"]
            - 0.5 * r["PF"]
        )
        values.append(raw / r["MIN"] * 48.0)
    return sum(values) / len(values)


async def compute_and_store_season(season: str) -> None:
    season_year = _season_start_year(season)
    print(f"computing key players for {season} (season_year={season_year})...")

    rows = _fetch_season_player_rows(season)
    league_avg_raw_per48 = _league_avg_raw_per48(rows)

    for row in rows:
        row["ws_48"] = compute_ws48_approx(row, row["team_win_pct"])
        row["per"] = compute_uper(row, league_avg_raw_per48)
        row["mpg"] = row["MIN"]

    by_team: dict[str, list[dict]] = {}
    for row in rows:
        by_team.setdefault(row["TEAM_ABBREVIATION"], []).append(row)

    now = datetime.now(UTC)
    written, skipped_teams = 0, 0
    async with async_session_factory() as db:
        for abbreviation, players in by_team.items():
            team = (
                await db.execute(select(Team).where(Team.short_name == abbreviation))
            ).scalar_one_or_none()
            if team is None:
                skipped_teams += 1  # Team rows are created lazily by fixture ingestion
                continue

            top5 = select_top5(
                [
                    {
                        "player_id": p["PLAYER_ID"],
                        "player_name": p["PLAYER_NAME"],
                        "mpg": p["mpg"],
                        "ws_48": p["ws_48"],
                        "per": p["per"],
                    }
                    for p in players
                ]
            )

            await db.execute(
                delete(TeamKeyPlayer).where(
                    TeamKeyPlayer.team_id == team.id, TeamKeyPlayer.season_year == season_year
                )
            )
            for rank, player in enumerate(top5, start=1):
                db.add(
                    TeamKeyPlayer(
                        team_id=team.id,
                        season_year=season_year,
                        player_rank=rank,
                        player_id=player["player_id"],
                        player_name=player["player_name"],
                        rank_metric=player["ws_48"],
                        combined_metric=player["per"],
                        mpg=player["mpg"],
                        computed_at=now,
                    )
                )
            written += len(top5)

        await db.commit()

    print(f"  {written} team_key_players rows written, {skipped_teams} teams skipped (no Team row yet)")


async def main_async() -> None:
    for season in SEASONS:
        await compute_and_store_season(season)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
