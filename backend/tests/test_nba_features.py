"""Unit tests for app/models_ml/nba_features.py's assemble_from_game_log — pure pandas
computation, no network, no DB. The leakage-guard test is the important one: every rolling
stat must be computed from games strictly before as_of_date, never including the game being
predicted itself."""

from datetime import date

import pandas as pd
import pytest

from app.models_ml.nba_features import FEATURE_NAMES, assemble_from_game_log

SEASON = "2024"


def make_games_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"]).dt.date
    return df


# Shared history used by most tests below. HOME beat OTHER by 10 on 01-01, then lost to AWAY
# by 5 (back-to-back, 01-02). AWAY beat HOME by 5 that same game, then beat OTHER by 8 on
# 01-05. A game on 01-06 (the as_of_date used below) is deliberately included to verify it's
# excluded from every computed stat.
BASE_ROWS = [
    {
        "TEAM_ABBREVIATION": "HOME",
        "GAME_DATE": "2024-01-01",
        "MATCHUP": "HOME vs. OTHER",
        "WL": "W",
        "PLUS_MINUS": 10,
        "SEASON": SEASON,
    },
    {
        "TEAM_ABBREVIATION": "OTHER",
        "GAME_DATE": "2024-01-01",
        "MATCHUP": "OTHER @ HOME",
        "WL": "L",
        "PLUS_MINUS": -10,
        "SEASON": SEASON,
    },
    {
        "TEAM_ABBREVIATION": "HOME",
        "GAME_DATE": "2024-01-02",
        "MATCHUP": "HOME @ AWAY",
        "WL": "L",
        "PLUS_MINUS": -5,
        "SEASON": SEASON,
    },
    {
        "TEAM_ABBREVIATION": "AWAY",
        "GAME_DATE": "2024-01-02",
        "MATCHUP": "AWAY vs. HOME",
        "WL": "W",
        "PLUS_MINUS": 5,
        "SEASON": SEASON,
    },
    {
        "TEAM_ABBREVIATION": "AWAY",
        "GAME_DATE": "2024-01-05",
        "MATCHUP": "AWAY vs. OTHER",
        "WL": "W",
        "PLUS_MINUS": 8,
        "SEASON": SEASON,
    },
    {
        "TEAM_ABBREVIATION": "OTHER",
        "GAME_DATE": "2024-01-05",
        "MATCHUP": "OTHER @ AWAY",
        "WL": "L",
        "PLUS_MINUS": -8,
        "SEASON": SEASON,
    },
    # The game being "predicted" — must never leak into its own feature vector.
    {
        "TEAM_ABBREVIATION": "HOME",
        "GAME_DATE": "2024-01-06",
        "MATCHUP": "HOME vs. AWAY",
        "WL": "W",
        "PLUS_MINUS": 3,
        "SEASON": SEASON,
    },
    {
        "TEAM_ABBREVIATION": "AWAY",
        "GAME_DATE": "2024-01-06",
        "MATCHUP": "AWAY @ HOME",
        "WL": "L",
        "PLUS_MINUS": -3,
        "SEASON": SEASON,
    },
]


def test_feature_names_are_stable_and_ordered():
    # Regression guard: reordering/renaming FEATURE_NAMES silently breaks train/serve parity.
    assert FEATURE_NAMES == (
        "rest_days_home",
        "rest_days_away",
        "back_to_back_home",
        "back_to_back_away",
        "last10_win_rate_home",
        "last10_win_rate_away",
        "last10_point_diff_home",
        "last10_point_diff_away",
        "net_rating_diff",
        "home_court_indicator",
        "h2h_win_rate_home",
        "key_players_available_home",
        "key_players_available_away",
        "key_players_per_combined_home",
        "key_players_per_combined_away",
        "moneyline_implied_prob_home",
    )


def test_rest_days_and_back_to_back():
    games_df = make_games_df(BASE_ROWS)
    features = assemble_from_game_log(games_df, date(2024, 1, 6), SEASON, "HOME", "AWAY")

    assert features["rest_days_home"] == 4.0  # last HOME game before 01-06 was 01-02
    assert features["rest_days_away"] == 1.0  # last AWAY game before 01-06 was 01-05
    assert features["back_to_back_home"] == 0.0
    assert features["back_to_back_away"] == 1.0


def test_last10_and_net_rating():
    games_df = make_games_df(BASE_ROWS)
    features = assemble_from_game_log(games_df, date(2024, 1, 6), SEASON, "HOME", "AWAY")

    assert features["last10_win_rate_home"] == pytest.approx(0.5)  # 1 W, 1 L prior to 01-06
    assert features["last10_win_rate_away"] == pytest.approx(1.0)  # 2 W prior to 01-06
    assert features["last10_point_diff_home"] == pytest.approx((10 + -5) / 2)
    assert features["last10_point_diff_away"] == pytest.approx((5 + 8) / 2)
    assert features["net_rating_diff"] == pytest.approx(((10 + -5) / 2) - ((5 + 8) / 2))


def test_h2h_and_constants():
    games_df = make_games_df(BASE_ROWS)
    features = assemble_from_game_log(
        games_df, date(2024, 1, 6), SEASON, "HOME", "AWAY", moneyline_implied_prob_home=0.6
    )

    # HOME's only prior meeting with AWAY (01-02) was a loss.
    assert features["h2h_win_rate_home"] == pytest.approx(0.0)
    assert features["home_court_indicator"] == 1.0
    assert features["moneyline_implied_prob_home"] == 0.6


def test_leakage_guard_excludes_as_of_date_game():
    """The 01-06 game itself (HOME won by 3) must never influence its own feature vector —
    if the date filter used <= instead of <, last10/rest/h2h would all change."""
    games_df = make_games_df(BASE_ROWS)
    features = assemble_from_game_log(games_df, date(2024, 1, 6), SEASON, "HOME", "AWAY")

    # If the 01-06 game leaked in, HOME's last10 win rate would be 2/3 (0.667), not 0.5, and
    # h2h_win_rate_home would be 0.5 (1 win, 1 loss) instead of 0.0.
    assert features["last10_win_rate_home"] == pytest.approx(0.5)
    assert features["h2h_win_rate_home"] == pytest.approx(0.0)
    assert features["rest_days_home"] == 4.0  # not 0.0


def test_missing_history_returns_none_not_a_fabricated_default():
    games_df = make_games_df(BASE_ROWS)
    features = assemble_from_game_log(games_df, date(2024, 1, 6), SEASON, "NEWTEAM", "AWAY")

    assert features["rest_days_home"] is None
    assert features["last10_win_rate_home"] is None
    assert features["last10_point_diff_home"] is None
    assert features["net_rating_diff"] is None  # one side missing -> the diff is missing too
    assert features["back_to_back_home"] == 0.0  # unknown defaults to "not back-to-back"
    assert features["h2h_win_rate_home"] is None  # NEWTEAM has no games at all, let alone H2H


def test_moneyline_defaults_to_none_when_not_provided():
    games_df = make_games_df(BASE_ROWS)
    features = assemble_from_game_log(games_df, date(2024, 1, 6), SEASON, "HOME", "AWAY")
    assert features["moneyline_implied_prob_home"] is None
