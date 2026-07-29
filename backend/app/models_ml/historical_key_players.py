"""Shared box-score/lineup-presence key-player-availability logic for ONE-OFF PAST-GAME
RETRODICTION ONLY — used by ml/training/train_football.py's backtest-label construction AND
app/workers/backfill_predictions.py's real retrodicted predictions for completed fixtures.

This is deliberately NOT the live Stage 2 path (app/models_ml/key_player_availability.py,
which reads only player_injury_status, a pre-game signal). Real box-score/lineup presence
("who literally played") is target-adjacent — knowing who played is only leakage-free once the
outcome is already known and being predicted is a backtest exercise, not a forecast. The user
explicitly authorized this trade for retrodiction specifically (adopted from the Big5/Big3
approach in their own prior NBA notebook, "Running - NBA Games Prediction Project.ipynb") —
mirrors this codebase's existing historical_key_player_availability/get_key_player_availability
separation for NBA (see ml/training/train_nba.py vs app/models_ml/key_player_availability.py).

Kept in its own module, out of key_player_availability.py, specifically so it can never be
imported into the live Stage 2 path by accident.
"""

import pandas as pd


def index_played_names(lineups: pd.DataFrame) -> dict[tuple, set[str]]:
    """(FIXTURE_ID, TEAM_ID) -> lowercased names who appeared with real minutes. FIXTURE_ID/
    TEAM_ID here are whatever type the caller's lineups DataFrame uses (int for the cached
    training parquet, str for a live one-off fetch) — callers must be consistent about which
    type they look keys up with."""
    index: dict[tuple, set[str]] = {}
    for fixture_id, team_id, name in zip(
        lineups["FIXTURE_ID"], lineups["TEAM_ID"], lineups["PLAYER_NAME"].str.lower(), strict=False
    ):
        index.setdefault((fixture_id, team_id), set()).add(name)
    return index


def historical_key_player_availability(
    played_names_index: dict[tuple, set[str]],
    team_key_players_by_team_season: dict,
    team_id,
    season: int,
    fixture_id,
) -> tuple[int | None, float | None]:
    """BACKTEST/RETRODICTION LABEL ONLY, built from lineup presence in an already-completed
    fixture. team_key_players_by_team_season: {(team_external_id, season_year): [{"player_name",
    "combined_metric"}, ...]} — Stage 1 rows (ml/training/compute_football_key_players.py)."""
    key_players = team_key_players_by_team_season.get((team_id, season))
    if not key_players:
        return None, None

    played_names = played_names_index.get((fixture_id, team_id), set())

    available_count = 0
    combined = 0.0
    for key_player in key_players:
        if key_player["player_name"].lower() in played_names:
            available_count += 1
            combined += key_player["combined_metric"]
    return available_count, combined


async def load_team_key_players_by_team_season(db) -> dict[tuple, list[dict]]:
    """Real team_key_players rows (Stage 1), joined to each team's own external_id — see
    ml/training/train_football.py:_load_team_key_players, which this replaces (that function
    now delegates here so training and retrodiction share one real implementation instead of
    two copies drifting apart)."""
    from sqlalchemy import select

    from app.fixtures.models import Team, TeamKeyPlayer

    by_team_season: dict[tuple, list[dict]] = {}
    rows = (
        await db.execute(
            select(TeamKeyPlayer, Team.external_id).join(Team, Team.id == TeamKeyPlayer.team_id)
        )
    ).all()

    for key_player, external_id in rows:
        key = (external_id, key_player.season_year)
        by_team_season.setdefault(key, []).append(
            {
                "player_name": key_player.player_name,
                "combined_metric": key_player.combined_metric,
            }
        )
    return by_team_season
