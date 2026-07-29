import pytest

from app.picks.service import (
    best_available_odds,
    best_outcome,
    compute_expected_value,
)


def test_compute_expected_value_positive():
    # prob=0.6, odds=2.5 -> 0.6*1.5 - 0.4 = 0.9 - 0.4 = 0.5
    assert compute_expected_value(0.6, 2.5) == pytest.approx(0.5)


def test_compute_expected_value_negative():
    # prob=0.3, odds=1.5 -> 0.3*0.5 - 0.7 = 0.15 - 0.7 = -0.55
    assert compute_expected_value(0.3, 1.5) == pytest.approx(-0.55)


def test_best_available_odds_takes_max_per_market():
    odds_rows = [
        {"home_odds": 1.8, "draw_odds": 3.2, "away_odds": None},
        {"home_odds": 2.0, "draw_odds": 3.0, "away_odds": 4.5},
    ]
    best = best_available_odds(odds_rows)
    assert best == {"home": 2.0, "draw": 3.2, "away": 4.5}


def test_best_outcome_picks_highest_probability():
    outcome = best_outcome(
        home_prob=0.55, draw_prob=0.25, away_prob=0.20, home_odds=1.9, draw_odds=3.4, away_odds=4.2
    )
    assert outcome.selection == "home"
    assert outcome.probability == 0.55
    assert outcome.odds == 1.9


def test_best_outcome_skips_missing_draw_for_two_outcome_sports():
    outcome = best_outcome(
        home_prob=0.4, draw_prob=None, away_prob=0.6, home_odds=2.1, draw_odds=None, away_odds=1.7
    )
    assert outcome.selection == "away"
