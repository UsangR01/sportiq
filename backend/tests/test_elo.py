"""app/models_ml/elo.py — the real, iterative Elo rating (adopted from the user's own prior
NBA notebook), and its compute_elo_history walk over a full games_df."""

import pandas as pd

from app.models_ml.elo import INITIAL_ELO, apply_match_result, compute_elo_history, expected_score


def test_expected_score_is_even_for_equal_ratings():
    assert expected_score(1500, 1500) == 0.5


def test_expected_score_favours_higher_rating():
    assert expected_score(1600, 1400) > 0.5
    assert expected_score(1400, 1600) < 0.5


def test_apply_match_result_home_win_raises_home_lowers_away():
    new_home, new_away = apply_match_result(1500, 1500, home_score=2, away_score=0)
    assert new_home > 1500
    assert new_away < 1500
    # zero-sum: the two changes are equal and opposite for equal starting ratings
    assert abs((new_home - 1500) + (new_away - 1500)) < 1e-9


def test_apply_match_result_draw_moves_ratings_toward_each_other():
    new_home, new_away = apply_match_result(1600, 1400, home_score=1, away_score=1)
    # the favourite (home) was expected to win outright; a draw is a below-expectation result
    assert new_home < 1600
    assert new_away > 1400


def test_apply_match_result_away_win():
    new_home, new_away = apply_match_result(1500, 1500, home_score=0, away_score=2)
    assert new_home < 1500
    assert new_away > 1500


def _games_row(fixture_id, season, date, team, opp, home_away, gf, ga, wdl):
    return {
        "FIXTURE_ID": fixture_id,
        "SEASON": season,
        "GAME_DATE": date,
        "TEAM_ID": team,
        "OPPONENT_ID": opp,
        "HOME_AWAY": home_away,
        "GF": gf,
        "GA": ga,
        "WDL": wdl,
    }


def test_compute_elo_history_first_meeting_starts_at_initial():
    games = pd.DataFrame(
        [
            _games_row(1, 2024, "2024-01-01", "A", "B", "home", 2, 0, "W"),
            _games_row(1, 2024, "2024-01-01", "B", "A", "away", 0, 2, "L"),
        ]
    )
    history = compute_elo_history(games)
    assert history[(1, "A")] == INITIAL_ELO
    assert history[(1, "B")] == INITIAL_ELO


def test_compute_elo_history_carries_state_across_fixtures_chronologically():
    games = pd.DataFrame(
        [
            _games_row(1, 2024, "2024-01-01", "A", "B", "home", 2, 0, "W"),
            _games_row(1, 2024, "2024-01-01", "B", "A", "away", 0, 2, "L"),
            _games_row(2, 2024, "2024-01-08", "A", "C", "home", 1, 1, "D"),
            _games_row(2, 2024, "2024-01-08", "C", "A", "away", 1, 1, "D"),
        ]
    )
    history = compute_elo_history(games)
    # A's rating going into fixture 2 must reflect the real result of fixture 1 (a win),
    # not reset back to INITIAL_ELO.
    assert history[(2, "A")] > INITIAL_ELO
    assert history[(1, "A")] == INITIAL_ELO


def test_compute_elo_history_only_uses_home_perspective_rows():
    """Each real match appears twice in games_df (home + away row) — compute_elo_history must
    process it exactly once, not double-update from seeing both rows."""
    games = pd.DataFrame(
        [
            _games_row(1, 2024, "2024-01-01", "A", "B", "home", 2, 0, "W"),
            _games_row(1, 2024, "2024-01-01", "B", "A", "away", 0, 2, "L"),
        ]
    )
    history = compute_elo_history(games)
    expected_home, expected_away = apply_match_result(INITIAL_ELO, INITIAL_ELO, 2, 0)
    # elo_pre (before fixture 1) is INITIAL_ELO for both — the update itself isn't exposed in
    # elo_pre, but confirms no double-application occurred by checking a downstream fixture.
    games_with_next = pd.concat(
        [
            games,
            pd.DataFrame(
                [
                    _games_row(2, 2024, "2024-01-08", "A", "C", "home", 0, 0, "D"),
                    _games_row(2, 2024, "2024-01-08", "C", "A", "away", 0, 0, "D"),
                ]
            ),
        ],
        ignore_index=True,
    )
    history2 = compute_elo_history(games_with_next)
    assert history2[(2, "A")] == expected_home
    assert history[(1, "A")] == INITIAL_ELO
