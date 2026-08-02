"""Unit tests for app/models_ml/tennis_features.py's assemble_from_game_log — pure pandas
computation, no network, no DB. Mirrors test_nba_features.py's shape; the leakage-guard test
is the important one, same rationale as that module."""

from datetime import date

import pandas as pd
import pytest

from app.models_ml.tennis_features import FEATURE_NAMES, assemble_from_game_log


def make_games_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"]).dt.date
    return df


# HOME beat OTHER on 01-01 (hard), lost to AWAY on 01-02 (hard, back-to-back with the game
# being predicted). AWAY beat HOME that same day, then beat OTHER on 01-05 (clay). A row on
# 01-06 (the as_of_date used below, on Hard) is deliberately included to verify it never
# leaks into its own feature vector.
BASE_ROWS = [
    {
        "PLAYER_ID": "HOME",
        "GAME_DATE": "2024-01-01",
        "OPPONENT_ID": "OTHER",
        "WL": "W",
        "SURFACE": "Hard",
    },
    {
        "PLAYER_ID": "OTHER",
        "GAME_DATE": "2024-01-01",
        "OPPONENT_ID": "HOME",
        "WL": "L",
        "SURFACE": "Hard",
    },
    {
        "PLAYER_ID": "HOME",
        "GAME_DATE": "2024-01-02",
        "OPPONENT_ID": "AWAY",
        "WL": "L",
        "SURFACE": "Hard",
    },
    {
        "PLAYER_ID": "AWAY",
        "GAME_DATE": "2024-01-02",
        "OPPONENT_ID": "HOME",
        "WL": "W",
        "SURFACE": "Hard",
    },
    {
        "PLAYER_ID": "AWAY",
        "GAME_DATE": "2024-01-05",
        "OPPONENT_ID": "OTHER",
        "WL": "W",
        "SURFACE": "Clay",
    },
    {
        "PLAYER_ID": "OTHER",
        "GAME_DATE": "2024-01-05",
        "OPPONENT_ID": "AWAY",
        "WL": "L",
        "SURFACE": "Clay",
    },
    # The match being "predicted" — must never leak into its own feature vector. Played on
    # Hard, so surface_win_rate should reflect Hard-only history.
    {
        "PLAYER_ID": "HOME",
        "GAME_DATE": "2024-01-06",
        "OPPONENT_ID": "AWAY",
        "WL": "W",
        "SURFACE": "Hard",
    },
    {
        "PLAYER_ID": "AWAY",
        "GAME_DATE": "2024-01-06",
        "OPPONENT_ID": "HOME",
        "WL": "L",
        "SURFACE": "Hard",
    },
]


def test_feature_names_are_stable_and_ordered():
    # Regression guard: reordering/renaming FEATURE_NAMES silently breaks train/serve parity.
    assert FEATURE_NAMES == (
        "rank_diff",
        "form_win_rate_home",
        "form_win_rate_away",
        "days_since_last_match_home",
        "days_since_last_match_away",
        "win_streak_home",
        "win_streak_away",
        "h2h_win_rate_home",
        "h2h_win_rate_surface_home",
        "surface_win_rate_home",
        "surface_win_rate_away",
        "surface_streak_home",
        "surface_streak_away",
        "moneyline_implied_prob_home",
    )


def test_rest_days_and_form():
    games_df = make_games_df(BASE_ROWS)
    features = assemble_from_game_log(games_df, date(2024, 1, 6), "HOME", "AWAY")

    assert features["days_since_last_match_home"] == 4.0  # last HOME match before 01-06: 01-02
    assert features["days_since_last_match_away"] == 1.0  # last AWAY match before 01-06: 01-05
    assert features["form_win_rate_home"] == pytest.approx(0.5)  # 1 W, 1 L prior to 01-06
    assert features["form_win_rate_away"] == pytest.approx(1.0)  # 2 W prior to 01-06


def test_win_streak():
    games_df = make_games_df(BASE_ROWS)
    features = assemble_from_game_log(games_df, date(2024, 1, 6), "HOME", "AWAY")

    # HOME's most recent prior match (01-02) was a loss -> losing streak, not a win streak.
    assert features["win_streak_home"] == 0.0
    # AWAY's two prior matches (01-02, 01-05) were both wins.
    assert features["win_streak_away"] == 2.0


def test_h2h_win_rate():
    games_df = make_games_df(BASE_ROWS)
    features = assemble_from_game_log(games_df, date(2024, 1, 6), "HOME", "AWAY")
    # HOME's only prior meeting with AWAY (01-02) was a loss.
    assert features["h2h_win_rate_home"] == pytest.approx(0.0)


def test_surface_win_rate_filters_to_current_matchs_surface():
    games_df = make_games_df(BASE_ROWS)
    features = assemble_from_game_log(games_df, date(2024, 1, 6), "HOME", "AWAY")

    # The 01-06 match is on Hard. HOME's prior Hard matches: 01-01 (W), 01-02 (L) -> 0.5.
    assert features["surface_win_rate_home"] == pytest.approx(0.5)
    # AWAY's prior Hard matches: only 01-02 (W) -> 1.0 (01-05 was Clay, excluded).
    assert features["surface_win_rate_away"] == pytest.approx(1.0)


def test_rank_diff_and_moneyline_passthrough():
    games_df = make_games_df(BASE_ROWS)
    features = assemble_from_game_log(
        games_df,
        date(2024, 1, 6),
        "HOME",
        "AWAY",
        home_rank_points=5000.0,
        away_rank_points=3000.0,
        moneyline_implied_prob_home=0.6,
    )
    assert features["rank_diff"] == pytest.approx(2000.0)
    assert features["moneyline_implied_prob_home"] == 0.6


def test_leakage_guard_excludes_as_of_date_match():
    """The 01-06 match itself (HOME won) must never influence its own feature vector — if the
    date filter used <= instead of <, form/h2h/streak would all change."""
    games_df = make_games_df(BASE_ROWS)
    features = assemble_from_game_log(games_df, date(2024, 1, 6), "HOME", "AWAY")

    # If 01-06 leaked in, HOME's form would be 2/3 (0.667) and win_streak would be 1, not 0.
    assert features["form_win_rate_home"] == pytest.approx(0.5)
    assert features["win_streak_home"] == 0.0
    assert features["days_since_last_match_home"] == 4.0  # not 0.0


def test_missing_history_returns_none_not_a_fabricated_default():
    games_df = make_games_df(BASE_ROWS)
    features = assemble_from_game_log(games_df, date(2024, 1, 6), "NEWPLAYER", "AWAY")

    assert features["days_since_last_match_home"] is None
    assert features["form_win_rate_home"] is None
    assert features["win_streak_home"] is None
    assert features["h2h_win_rate_home"] is None
    assert features["h2h_win_rate_surface_home"] is None
    assert features["surface_win_rate_home"] is None
    assert features["surface_streak_home"] is None


# A separate, small dataset specifically for distinguishing overall H2H/streak from their
# surface-conditioned counterparts — added per direct user request ("when considering H2H
# between players, also consider H2H on the particular surface... also their streak on the
# surface separately"). HOME beat AWAY on Clay in mid-2023, then lost to AWAY on Hard in
# early January — overall H2H and Hard-only H2H must therefore differ.
SURFACE_H2H_ROWS = [
    {
        "PLAYER_ID": "HOME",
        "GAME_DATE": "2023-06-01",
        "OPPONENT_ID": "AWAY",
        "WL": "W",
        "SURFACE": "Clay",
    },
    {
        "PLAYER_ID": "AWAY",
        "GAME_DATE": "2023-06-01",
        "OPPONENT_ID": "HOME",
        "WL": "L",
        "SURFACE": "Clay",
    },
    {
        "PLAYER_ID": "HOME",
        "GAME_DATE": "2023-12-01",
        "OPPONENT_ID": "OTHER",
        "WL": "W",
        "SURFACE": "Hard",
    },
    {
        "PLAYER_ID": "HOME",
        "GAME_DATE": "2024-01-02",
        "OPPONENT_ID": "AWAY",
        "WL": "L",
        "SURFACE": "Hard",
    },
    {
        "PLAYER_ID": "AWAY",
        "GAME_DATE": "2024-01-02",
        "OPPONENT_ID": "HOME",
        "WL": "W",
        "SURFACE": "Hard",
    },
    # The match being predicted, on Hard.
    {
        "PLAYER_ID": "HOME",
        "GAME_DATE": "2024-01-06",
        "OPPONENT_ID": "AWAY",
        "WL": "W",
        "SURFACE": "Hard",
    },
    {
        "PLAYER_ID": "AWAY",
        "GAME_DATE": "2024-01-06",
        "OPPONENT_ID": "HOME",
        "WL": "L",
        "SURFACE": "Hard",
    },
]


def test_h2h_win_rate_on_surface_differs_from_overall():
    games_df = make_games_df(SURFACE_H2H_ROWS)
    features = assemble_from_game_log(games_df, date(2024, 1, 6), "HOME", "AWAY")

    # Overall: 1 win (Clay, 2023-06-01), 1 loss (Hard, 2024-01-02) -> 0.5.
    assert features["h2h_win_rate_home"] == pytest.approx(0.5)
    # Hard-only: just the 2024-01-02 loss -> 0.0. Genuinely different from the overall figure.
    assert features["h2h_win_rate_surface_home"] == pytest.approx(0.0)


def test_surface_streak_is_independent_of_overall_streak():
    games_df = make_games_df(SURFACE_H2H_ROWS)
    features = assemble_from_game_log(games_df, date(2024, 1, 6), "HOME", "AWAY")

    # HOME's most recent prior Hard match (01-02) was a loss -> no active Hard win streak,
    # even though HOME's overall most-recent-anything streak calc is a separate question.
    assert features["surface_streak_home"] == 0.0
    # AWAY's only prior Hard match (01-02) was a win -> a streak of 1.
    assert features["surface_streak_away"] == 1.0


def test_rank_diff_none_when_either_side_missing():
    games_df = make_games_df(BASE_ROWS)
    features = assemble_from_game_log(
        games_df, date(2024, 1, 6), "HOME", "AWAY", home_rank_points=5000.0
    )
    assert features["rank_diff"] is None


def test_moneyline_defaults_to_none_when_not_provided():
    games_df = make_games_df(BASE_ROWS)
    features = assemble_from_game_log(games_df, date(2024, 1, 6), "HOME", "AWAY")
    assert features["moneyline_implied_prob_home"] is None
