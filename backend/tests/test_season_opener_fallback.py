"""A season opener is not an unknown, and serving it as one was a train/serve mismatch.

REPORTED: no predictions surfaced for the EPL's opening weekend. Measured on production: all
ten fixtures carried the model's flat prior (H0.684 D0.232 A0.083 — Arsenal v promoted
Coventry and Hull City v Manchester United, identical numbers), and the completeness floor
correctly hid every pick. The floor was right; the vector was the defect.

At a season opening /teams/statistics has nothing, so the live path yields all-None. But
TRAINING ROLLS WINDOWS ACROSS SEASON BOUNDARIES — assemble_from_game_log filters on GAME_DATE
alone, never season — so a team's first fixture of a season trained on the tail of its last.
The model has never seen "established club, empty vector"; serving invented that case. The
game logs ship in the image precisely so past-facing features can be real.

Measured after the fallback, same ten fixtures: 8 of 10 surface with differentiated
probabilities (Man City 0.58 home, Newcastle v Liverpool 0.37/0.36). The two still hidden are
promoted-vs-established pairings whose vectors are honestly half-empty.
"""

import pandas as pd

from app.models_ml.elo import INITIAL_ELO
from app.workers.backfill_predictions import final_elo_ratings
from app.workers.run_predictions import _FOOTBALL_CORE_SIGNALS, _football_vector_is_empty


def vector(**overrides):
    base = {name: None for name in _FOOTBALL_CORE_SIGNALS}
    base.update(overrides)
    return base


def test_an_all_none_vector_triggers_the_fallback():
    assert _football_vector_is_empty(vector())


def test_one_populated_side_does_not_trigger_a_rebuild():
    """An established club hosting a promoted one is a REAL partial vector — exactly what
    training saw for promoted sides. Rebuilding it would replace honest asymmetry with a
    second opinion."""
    assert not _football_vector_is_empty(vector(attack_str_home=1.5))
    assert not _football_vector_is_empty(vector(form_pts_away=1.0))


def test_seeded_elo_alone_still_counts_as_empty():
    """Measured on production 2026-08-19: seeding five-season Elo made elo_diff real on every
    fixture, which silently disabled this fallback for whole leagues — 26 predictions built
    from Elo-and-nothing vectors (completeness 0.118, draw probabilities near 0.50). Elo is
    history, not evidence the season has data; only the rolling-form signals answer that."""
    assert _football_vector_is_empty(vector(elo_diff=32.0))


def _log(rows):
    return pd.DataFrame(
        rows,
        columns=["FIXTURE_ID", "TEAM_ID", "OPPONENT_ID", "HOME_AWAY", "GF", "GA", "GAME_DATE"],
    )


def test_final_elo_walk_ends_where_settlement_would_continue():
    """The winner of every match ends above the loser, from the same walk settlement uses."""
    log = _log(
        [
            ("1", "A", "B", "home", 2, 0, "2026-01-01"),
            ("1", "B", "A", "away", 0, 2, "2026-01-01"),
            ("2", "B", "A", "home", 0, 1, "2026-01-08"),
            ("2", "A", "B", "away", 1, 0, "2026-01-08"),
        ]
    )
    ratings = final_elo_ratings(log)

    assert ratings["A"] > INITIAL_ELO > ratings["B"]
    # Zero-sum by construction: what A gained, B lost.
    assert abs((ratings["A"] - INITIAL_ELO) + (ratings["B"] - INITIAL_ELO)) < 1e-9


def test_only_home_rows_are_walked_so_no_match_counts_twice():
    """The log carries one row per team per fixture; walking both would double every update."""
    single = _log(
        [
            ("1", "A", "B", "home", 3, 0, "2026-01-01"),
            ("1", "B", "A", "away", 0, 3, "2026-01-01"),
        ]
    )
    double_home = _log(
        [
            ("1", "A", "B", "home", 3, 0, "2026-01-01"),
            ("1", "B", "A", "away", 0, 3, "2026-01-01"),
            ("2", "A", "B", "home", 3, 0, "2026-02-01"),
            ("2", "B", "A", "away", 0, 3, "2026-02-01"),
        ]
    )
    once = final_elo_ratings(single)["A"]
    twice = final_elo_ratings(double_home)["A"]

    assert twice > once, "two wins must move further than one"
    assert once - INITIAL_ELO < (twice - INITIAL_ELO), "sanity on direction"


def test_a_team_absent_from_the_log_is_simply_absent():
    """The caller substitutes INITIAL_ELO for an unknown team — matching training, where the
    Elo walk starts every team at 1500 on first appearance. A promoted side's first top-flight
    fixture trained with elo_diff = opponent - 1500, never with a missing value."""
    ratings = final_elo_ratings(
        _log(
            [
                ("1", "A", "B", "home", 1, 0, "2026-01-01"),
                ("1", "B", "A", "away", 0, 1, "2026-01-01"),
            ]
        )
    )
    assert "PROMOTED" not in ratings
