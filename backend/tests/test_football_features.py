"""app/models_ml/football_features.py — the shared training/serving feature-vector assembly.
Covers the new Elo/streak/richer-H2H additions; the pre-existing rolling-form/rest-days/
win-rate logic already has coverage elsewhere in this file's history (kept minimal here, not
re-tested for behavior that hasn't changed)."""

from datetime import date

import pandas as pd

from app.models_ml.football_features import FEATURE_NAMES, assemble_from_game_log


def _row(team, opp, home_away, game_date, gf, ga, wdl):
    return {
        "TEAM_ID": team,
        "OPPONENT_ID": opp,
        "HOME_AWAY": home_away,
        "GAME_DATE": game_date,
        "GF": gf,
        "GA": ga,
        "WDL": wdl,
    }


def test_feature_names_includes_new_features():
    assert "elo_diff" in FEATURE_NAMES
    assert "win_streak_home" in FEATURE_NAMES
    assert "win_streak_away" in FEATURE_NAMES
    assert "h2h_avg_goals_scored_home" in FEATURE_NAMES
    assert "h2h_avg_goals_allowed_home" in FEATURE_NAMES


def test_assemble_from_game_log_passes_through_elo_diff():
    games = pd.DataFrame([_row("A", "B", "home", date(2024, 1, 1), 1, 0, "W")])
    features = assemble_from_game_log(games, date(2024, 1, 8), "A", "B", elo_diff=42.5)
    assert features["elo_diff"] == 42.5


def test_assemble_from_game_log_elo_diff_none_by_default():
    games = pd.DataFrame([_row("A", "B", "home", date(2024, 1, 1), 1, 0, "W")])
    features = assemble_from_game_log(games, date(2024, 1, 8), "A", "B")
    assert features["elo_diff"] is None


def test_assemble_from_game_log_win_streak_home():
    games = pd.DataFrame(
        [
            _row("A", "X", "home", date(2024, 1, 1), 1, 0, "W"),
            _row("A", "Y", "away", date(2024, 1, 8), 2, 0, "W"),
            _row("A", "Z", "home", date(2024, 1, 15), 3, 0, "W"),
        ]
    )
    features = assemble_from_game_log(games, date(2024, 1, 22), "A", "B")
    assert features["win_streak_home"] == 3.0


def test_assemble_from_game_log_win_streak_broken_by_loss():
    games = pd.DataFrame(
        [
            _row("A", "X", "home", date(2024, 1, 1), 1, 0, "W"),
            _row("A", "Y", "away", date(2024, 1, 8), 0, 2, "L"),
        ]
    )
    features = assemble_from_game_log(games, date(2024, 1, 15), "A", "B")
    assert features["win_streak_home"] == 0.0


def test_assemble_from_game_log_win_streak_none_with_no_history():
    games = pd.DataFrame(
        [], columns=["TEAM_ID", "OPPONENT_ID", "HOME_AWAY", "GAME_DATE", "GF", "GA", "WDL"]
    )
    features = assemble_from_game_log(games, date(2024, 1, 22), "A", "B")
    assert features["win_streak_home"] is None
    assert features["win_streak_away"] is None


def test_assemble_from_game_log_h2h_avg_goals():
    games = pd.DataFrame(
        [
            _row("A", "B", "home", date(2023, 1, 1), 3, 1, "W"),
            _row("A", "B", "home", date(2023, 6, 1), 1, 1, "D"),
        ]
    )
    features = assemble_from_game_log(games, date(2024, 1, 1), "A", "B")
    assert features["h2h_win_rate_home"] == 0.5
    assert features["h2h_avg_goals_scored_home"] == 2.0
    assert features["h2h_avg_goals_allowed_home"] == 1.0


def test_assemble_from_game_log_h2h_none_with_no_meetings():
    games = pd.DataFrame(
        [], columns=["TEAM_ID", "OPPONENT_ID", "HOME_AWAY", "GAME_DATE", "GF", "GA", "WDL"]
    )
    features = assemble_from_game_log(games, date(2024, 1, 1), "A", "B")
    assert features["h2h_win_rate_home"] is None
    assert features["h2h_avg_goals_scored_home"] is None
    assert features["h2h_avg_goals_allowed_home"] is None


def test_assemble_from_game_log_leakage_guard_excludes_future_games():
    games = pd.DataFrame(
        [
            _row("A", "X", "home", date(2024, 1, 1), 1, 0, "W"),
            _row("A", "Y", "home", date(2024, 6, 1), 0, 5, "L"),  # strictly AFTER as_of_date
        ]
    )
    features = assemble_from_game_log(games, date(2024, 2, 1), "A", "B")
    # Only the Jan 1 win is visible as of Feb 1 — the June loss must not affect the streak.
    assert features["win_streak_home"] == 1.0
