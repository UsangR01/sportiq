"""The pre-registered trigger for re-admitting goals_total to the headline pick.

The bar in app/fixtures/router.py:NO_DEMONSTRATED_SIGNAL_MARKETS was imposed on one live
measurement: predicted total goals vs actual, r=+0.049 on n=242, against corners' r=+0.288
which kept its place. Layer 1 tuning later improved the market's TEST-SET discrimination a lot
(under-3.5 trend z +5.97 -> +8.79). That is different evidence on a different population, so
the trigger stays defined on live settled predictions and is fixed in advance.

These tests pin the thresholds and, more importantly, the reasons they cannot be met cheaply.
"""

from app.predictions.market_signal import (
    MIN_CI_LOW,
    MIN_MODEL_VERSION,
    MIN_N,
    MIN_R,
    MarketSignal,
    pearson_with_ci,
)


def _signal(n, r, lo, hi=0.9):
    return MarketSignal(market="goals_total", n=n, r=r, ci_low=lo, ci_high=hi)


def test_the_barred_measurement_would_not_meet_the_trigger():
    """The number that got goals_total barred must fail its own re-admission test, or the
    trigger is decorative."""
    assert not _signal(242, 0.049, -0.077).meets_trigger


def test_a_strong_correlation_on_a_small_sample_is_refused():
    """This is the failure mode the interval exists for. r=+0.40 looks decisive and on n=30 the
    interval reaches almost to zero — exactly the mistake made earlier today, when a per-league
    read on n=12-96 showed +26pp and -26.7pp whose signs both flipped on real data."""
    assert not _signal(30, 0.40, 0.04).meets_trigger


def test_a_big_sample_with_a_weak_correlation_is_refused():
    """n alone is not evidence: 5,000 fixtures confidently showing near-nothing is still
    near-nothing, and is precisely the state the market was barred in."""
    assert not _signal(5000, 0.06, 0.03).meets_trigger


def test_all_three_conditions_together_pass():
    assert _signal(250, 0.22, 0.10).meets_trigger


def test_the_interval_must_exclude_the_level_that_got_it_barred():
    """r clears MIN_R and n clears MIN_N, but the interval still reaches down to the barred
    level — so the sample does not actually distinguish 'improved' from 'unchanged'."""
    assert not _signal(250, 0.16, MIN_CI_LOW).meets_trigger


def test_only_the_tuned_model_onward_counts():
    """Every one of the original 242 came from the pre-tuning model. Including them would
    measure the model being replaced. Versions sort lexicographically by design."""
    assert MIN_MODEL_VERSION.startswith("football_xgb_v")
    assert "football_xgb_v20260810092634" < MIN_MODEL_VERSION
    assert "football_xgb_v20260901000000" > MIN_MODEL_VERSION


def test_thresholds_are_where_the_reasoning_put_them():
    """Anchored, not picked: MIN_R sits between the +0.049 that failed and the +0.288 that
    corners passed on, and MIN_CI_LOW is the level that got the market barred."""
    assert MIN_N == 200
    assert 0.049 < MIN_R < 0.288
    assert MIN_CI_LOW == 0.05


def test_pearson_matches_a_known_value_and_widens_its_interval_on_small_n():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    r, lo, hi = pearson_with_ci(xs, [2.0, 4.0, 6.0, 8.0, 10.0])
    # Just below 1.0, not equal to it: the Fisher transform is atanh, which is infinite at
    # exactly 1, so r is clamped. That clamp is deliberate and this pins it.
    assert 0.9999 < r < 1.0

    big_r, big_lo, big_hi = pearson_with_ci(
        [float(i) for i in range(200)], [float(i % 7) for i in range(200)]
    )
    small_r, small_lo, small_hi = pearson_with_ci(
        [float(i) for i in range(10)], [float(i % 7) for i in range(10)]
    )
    assert (big_hi - big_lo) < (small_hi - small_lo)


def test_too_few_points_returns_nothing_rather_than_a_number():
    assert pearson_with_ci([1.0, 2.0], [1.0, 2.0]) == (None, None, None)


def test_the_check_reports_but_does_not_lift_the_bar_itself():
    """Re-admitting a market to the headline pick changes what users are told to back. That is
    a product decision and must not happen quietly on a schedule."""
    from pathlib import Path

    body = (
        Path(__file__).resolve().parents[1] / "app" / "workers" / "check_market_signal.py"
    ).read_text(encoding="utf-8")
    assert "NO_DEMONSTRATED_SIGNAL_MARKETS" in body  # named, so the reader knows where to look
    assert "remove" in body.lower()
    # It must not import or mutate the live pick-selection set.
    assert "from app.fixtures.router import" not in body
