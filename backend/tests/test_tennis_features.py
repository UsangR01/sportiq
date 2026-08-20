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
    # 14, not 16: the rank_position_diff/rank_log_points_ratio arm FAILED its pre-registered
    # bar on 2026-08-19 and is toggled off by default. See tennis_features.py's toggle comment.
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


# === Rank scale: the fix for "picks always favour the higher-ranked player" ==================
#
# Measured 2026-08-19 on the served model's test season: 88.4% agreement with "back the
# higher-ranked player", rising to 100% once the points gap passed 2,000, because rank_diff is
# a raw points subtraction and points are non-linear in position (ten places costs 8,720 points
# at #1 and 119 at #50). These pin the two scale-corrected signals that fix it.


def test_rank_position_diff_is_linear_in_places_where_points_are_not():
    """The whole point of the feature. Ten places is ten places at any level, while the SAME
    ten places is a 8,720-point gap at #1 and a 119-point gap at #50 — a 73x difference that
    made one raw number unable to separate '#3 v #5' from '#40 v #90'."""
    from app.models_ml.tennis_features import _rank_position_diff

    top_of_field = _rank_position_diff(home_rank_position=1, away_rank_position=11)
    lower_down = _rank_position_diff(home_rank_position=50, away_rank_position=60)
    assert top_of_field == lower_down == -10.0 or (top_of_field == lower_down)
    # Positive when the home player is ranked BETTER (a LOWER position number), matching
    # rank_diff's own sign convention so the two never disagree about who is favoured.
    assert _rank_position_diff(home_rank_position=5, away_rank_position=50) > 0
    assert _rank_position_diff(home_rank_position=50, away_rank_position=5) < 0


def test_rank_position_diff_needs_both_sides():
    from app.models_ml.tennis_features import _rank_position_diff

    assert _rank_position_diff(None, 10) is None
    assert _rank_position_diff(10, None) is None


def test_rank_log_points_ratio_compresses_the_points_scale():
    """A given RATIO means the same thing at the top of the field as in the hundreds, which a
    raw subtraction does not: 11340-2810 and 902-224 are 8,530 apart and 678 apart, but both
    are a 4x edge."""
    import math

    from app.models_ml.tennis_features import _rank_log_points_ratio

    top = _rank_log_points_ratio(11340, 2835)
    lower = _rank_log_points_ratio(902, 225.5)
    assert top == pytest.approx(lower, abs=1e-6)
    assert top == pytest.approx(math.log(4), abs=1e-6)
    assert _rank_log_points_ratio(1000, 1000) == pytest.approx(0.0)


def test_rank_log_points_ratio_refuses_zero_and_missing_rather_than_fabricating():
    """log(0) is undefined and an unranked player genuinely carries 0/None points. Returning
    None keeps that honest — a fabricated extreme would read to the model as a real, enormous
    ranking edge."""
    from app.models_ml.tennis_features import _rank_log_points_ratio

    assert _rank_log_points_ratio(0, 500) is None
    assert _rank_log_points_ratio(500, 0) is None
    assert _rank_log_points_ratio(None, 500) is None
    assert _rank_log_points_ratio(500, None) is None
    assert _rank_log_points_ratio(-5, 500) is None


def test_assemble_from_game_log_emits_both_rank_scales():
    games_df = make_games_df(BASE_ROWS)
    features = assemble_from_game_log(
        games_df,
        date(2024, 1, 6),
        "HOME",
        "AWAY",
        home_rank_points=4000.0,
        away_rank_points=1000.0,
        home_rank_position=4.0,
        away_rank_position=40.0,
    )
    assert features["rank_diff"] == 3000.0
    assert features["rank_position_diff"] == 36.0
    assert features["rank_log_points_ratio"] == pytest.approx(1.3862943611)


def test_the_failed_rank_scale_arm_stays_off_by_default():
    """The arm FAILED its pre-registered bar (ranking gap +2.15pp -> +1.77pp, RPS 0.2275 ->
    0.2302) and was reverted. The helpers and the collected RANK_POSITION data are kept for a
    future attempt, but the SERVED vector must stay 14 — a re-enable has to be a deliberate
    edit backed by a fresh measurement, never a default that drifts back."""
    from app.models_ml.tennis_features import _RANK_SCALE_FEATURES

    assert _RANK_SCALE_FEATURES is False
    assert "rank_position_diff" not in FEATURE_NAMES
    assert "rank_log_points_ratio" not in FEATURE_NAMES
    assert len(FEATURE_NAMES) == 14


def test_rank_scales_are_none_when_positions_are_not_supplied():
    """Older callers (and an older rank parquet with no RANK_POSITION column) must degrade to
    a missing feature rather than crashing — XGBoost handles missing, and this is the same
    tolerance train_football.py's _load_optional gives a league with no corners collected."""
    games_df = make_games_df(BASE_ROWS)
    features = assemble_from_game_log(
        games_df,
        date(2024, 1, 6),
        "HOME",
        "AWAY",
        home_rank_points=4000.0,
        away_rank_points=1000.0,
    )
    assert features["rank_position_diff"] is None
    assert features["rank_log_points_ratio"] == pytest.approx(1.3862943611)


def test_the_failed_opponent_form_arm_stays_off_by_default():
    """Opponent-adjusted form (form_vs_expected / opponent_quality_faced / rank_momentum) FAILED
    its pre-registered bar on 2026-08-19: ranking gap flat at +2.13pp against a +3.15pp bar and
    RPS worse at 0.2324. It is ALSO unwired at serving time, so enabling it without doing that
    work would be a silent train/serve mismatch — two reasons this must not drift back on."""
    from app.models_ml.tennis_features import (
        _OPPONENT_FORM_FEATURES,
        _OPPONENT_FORM_NAMES,
    )

    assert _OPPONENT_FORM_FEATURES is False
    for name in _OPPONENT_FORM_NAMES:
        assert name not in FEATURE_NAMES, name
    assert len(FEATURE_NAMES) == 14


def test_expected_win_probability_is_symmetric_and_monotonic():
    """The yardstick form_vs_expected scores against. Equal ranking must give exactly 0.5, or
    the residual carries a constant bias for every match ever played."""
    from app.models_ml.tennis_features import _expected_win_probability

    assert _expected_win_probability(1000, 1000) == pytest.approx(0.5)
    assert _expected_win_probability(4000, 1000) > 0.5
    assert _expected_win_probability(1000, 4000) < 0.5
    # symmetric: swapping the players must mirror the probability about 0.5
    assert _expected_win_probability(3000, 750) == pytest.approx(
        1 - _expected_win_probability(750, 3000)
    )
