"""app/models_ml/markets.py — double chance derivation and Poisson-based Over/Under
probability math for the new football prediction markets (double chance, O/U goals,
O/U corners)."""

import math

import pytest

from app.models_ml.markets import (
    CORNERS_LINES,
    GOALS_LINES,
    double_chance_probs,
    over_under_probs,
)


def test_double_chance_probs_sums_home_and_draw():
    home_or_draw, away_or_draw = double_chance_probs(0.5, 0.3, 0.2)
    assert home_or_draw == pytest.approx(0.8)
    assert away_or_draw == pytest.approx(0.5)


def test_double_chance_probs_none_when_draw_missing():
    """Two-outcome sports (e.g. NBA) have no draw_prob at all — double chance is meaningless
    there, so both outputs must be None, not silently treating draw as 0."""
    home_or_draw, away_or_draw = double_chance_probs(0.6, None, 0.4)
    assert home_or_draw is None
    assert away_or_draw is None


def test_over_under_probs_none_when_expected_total_missing():
    result = over_under_probs(None, GOALS_LINES)
    assert result == {line: (None, None) for line in GOALS_LINES}


def test_over_under_probs_under_plus_over_sums_to_one():
    result = over_under_probs(2.7, GOALS_LINES)
    for line in GOALS_LINES:
        under_prob, over_prob = result[line]
        assert under_prob + over_prob == pytest.approx(1.0)


def test_over_under_probs_higher_line_has_higher_under_probability():
    """For a fixed expected total, P(under 3.5) must exceed P(under 1.5) — a monotonicity
    sanity check on the Poisson CDF usage."""
    result = over_under_probs(2.7, GOALS_LINES)
    under_1_5, _ = result[1.5]
    under_2_5, _ = result[2.5]
    under_3_5, _ = result[3.5]
    assert under_1_5 < under_2_5 < under_3_5


def test_over_under_probs_matches_hand_computed_poisson_cdf():
    # Poisson(2.0): P(X<=2) = e^-2 * (1 + 2 + 2) = 5e^-2 ≈ 0.6767
    expected_under_2_5 = 5 * math.exp(-2)
    result = over_under_probs(2.0, (2.5,))
    under_prob, over_prob = result[2.5]
    assert under_prob == pytest.approx(expected_under_2_5, rel=1e-6)
    assert over_prob == pytest.approx(1 - expected_under_2_5, rel=1e-6)


def test_over_under_probs_low_expected_total_favours_under():
    """A near-zero expected total (e.g. a defensive corners regressor output) should make
    Under overwhelmingly likely for the standard corners line."""
    result = over_under_probs(0.1, CORNERS_LINES)
    under_prob, over_prob = result[CORNERS_LINES[0]]
    assert under_prob > 0.99
    assert over_prob < 0.01


def test_over_under_probs_high_expected_total_favours_over():
    result = over_under_probs(15.0, CORNERS_LINES)
    under_prob, over_prob = result[CORNERS_LINES[0]]
    assert over_prob > 0.9


# === Corners are overdispersed; goals are not (2026-08-24) ========================================


def test_corners_use_a_wider_distribution_than_poisson():
    """Measured on 23,782 TRAIN-season fixtures: total corners have var/mean 1.189, so Poisson
    tails are too thin and extreme probabilities come out too extreme — exactly where picks are
    drawn from. A wider distribution must pull them toward the middle."""
    from app.models_ml.markets import CORNERS_DISPERSION, over_under_probs

    poisson_under, poisson_over = over_under_probs(11.5, (9.5,))[9.5]
    nb_under, nb_over = over_under_probs(11.5, (9.5,), CORNERS_DISPERSION)[9.5]

    assert poisson_over > nb_over, "the wider distribution must be less extreme"
    assert nb_over > 0.5, "and must not overshoot past the midpoint"


def test_goals_are_left_on_poisson_deliberately():
    """THE CONTRAST, and both halves are measured. Total goals came out at var/mean 1.003 over
    8,718 fixtures, which is why a Negative Binomial was investigated for goals and rejected —
    it would fit a dispersion that does not exist. Corners were never measured separately."""
    from app.models_ml.markets import over_under_probs

    default_under, _ = over_under_probs(2.7, (2.5,))[2.5]
    explicit_poisson, _ = over_under_probs(2.7, (2.5,), 1.0)[2.5]

    assert default_under == explicit_poisson


def test_underdispersion_falls_back_to_poisson_rather_than_coercing():
    """A Negative Binomial cannot represent var < mean at all, and 9 of 18 leagues measured
    UNDERdispersed on goals — so a dispersion below 1 must degrade, not raise or distort."""
    from app.models_ml.markets import over_under_probs

    assert over_under_probs(9.8, (9.5,), 0.85)[9.5] == over_under_probs(9.8, (9.5,), 1.0)[9.5]


def test_a_missing_total_is_still_never_fabricated():
    from app.models_ml.markets import CORNERS_DISPERSION, over_under_probs

    assert over_under_probs(None, (9.5,), CORNERS_DISPERSION)[9.5] == (None, None)
