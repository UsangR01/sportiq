"""The held-out instruments for the 1X2 market, and the per-fixture outputs behind comparisons.

Both exist because of the same recurring failure: a number that is only ever PRINTED cannot be
checked, and this project has repeatedly discovered a market was wrong long after shipping it.
Over/Under goals shipped visibly overconfident, corners shipped judged by MAE while the product
sold "P(under 9.5)". 1X2 -- the market the whole product is built on -- had RPS and nothing
else, so nobody could say whether fixtures called 55% home actually win 55% of the time.

Tested here rather than in ml/training/ because train_football.py imports xgboost, mlflow and
optuna at module scope and is therefore unreachable from CI. That is exactly why these live in
app/models_ml/evaluation.py.
"""

import numpy as np
import pandas as pd
import pytest

from app.models_ml.evaluation import (
    XG_FEATURE_COLUMNS,
    build_test_prediction_rows,
    expected_calibration_error,
    multiclass_calibration,
)
from app.models_ml.football import FootballModel

CLASSES = FootballModel.CLASSES


def test_a_perfectly_calibrated_forecaster_scores_zero_ece():
    """The anchor. 100 fixtures called at exactly 0.7 confidence, 70 of which come true."""
    probs = np.tile([0.7, 0.2, 0.1], (100, 1))
    y = np.array([0] * 70 + [1] * 30)
    assert expected_calibration_error(y, probs) == pytest.approx(0.0, abs=1e-9)


def test_a_confidently_wrong_forecaster_scores_the_full_gap():
    """Claims 0.9 on every fixture and is right on none of them, so the gap IS the ECE. This
    is the shape of the failure the instrument exists to catch -- the 99.7%-off-3-features
    case that motivated MIN_FEATURE_COMPLETENESS looked exactly like this."""
    probs = np.tile([0.9, 0.05, 0.05], (50, 1))
    y = np.full(50, 1)
    assert expected_calibration_error(y, probs) == pytest.approx(0.9, abs=1e-9)


def test_probability_of_exactly_one_is_weighted_not_dropped():
    """The final bin closes on the right. An off-by-one here would silently exclude the most
    overconfident fixtures in the set -- the ones the metric is most needed for."""
    probs = np.tile([1.0, 0.0, 0.0], (20, 1))
    assert expected_calibration_error(np.full(20, 2), probs) == pytest.approx(1.0, abs=1e-9)


def test_ece_can_be_near_zero_while_a_class_is_badly_miscalibrated():
    """WHY THE PER-CLASS BUCKETS EXIST ALONGSIDE ECE, rather than instead of it.

    Top-label ECE only ever sees the argmax class, so a systematic error confined to draws --
    which are almost never the argmax in football -- is invisible to it. Here home is called at
    0.6 and wins 60% of the time (well calibrated on the top label), while draw is called at
    0.3 and never happens at all. ECE is ~0; the draw bucket shows 0.000.
    """
    probs = np.tile([0.6, 0.3, 0.1], (100, 1))
    y = np.array([0] * 60 + [2] * 40)  # home 60%, away 40%, draw never
    metrics, _ = multiclass_calibration(y, probs, probs, CLASSES)
    assert metrics["test_ece"] < 0.01
    assert metrics["reliability_draw_0.3"] == pytest.approx(0.0)
    assert metrics["reliability_home_0.6"] == pytest.approx(0.6)


def test_calibrated_and_uncalibrated_are_both_reported():
    """Isotonic calibration is a step that is supposed to earn its place, and nothing measured
    whether it does. Reporting only the calibrated number makes that unanswerable."""
    y = np.array([0] * 60 + [1] * 20 + [2] * 20)
    good = np.tile([0.6, 0.2, 0.2], (100, 1))
    bad = np.tile([0.98, 0.01, 0.01], (100, 1))
    metrics, _ = multiclass_calibration(y, good, bad, CLASSES)
    assert metrics["test_log_loss"] < metrics["test_log_loss_uncalibrated"]
    assert metrics["test_brier_multiclass"] < metrics["test_brier_multiclass_uncalibrated"]


def test_multiclass_brier_uses_the_summed_form():
    """Ranges [0, 2], NOT the two-outcome form train_nba.py reports under the bare name
    'brier'. A silent switch between the two conventions would make an NBA and a football
    number look comparable when one is double the other."""
    probs = np.array([[1.0, 0.0, 0.0]])
    metrics, _ = multiclass_calibration(np.array([2]), probs, probs, CLASSES)
    assert metrics["test_brier_multiclass"] == pytest.approx(2.0)


def test_a_bucket_below_the_reporting_minimum_is_suppressed():
    """A rate over a handful of fixtures is noise wearing a decimal point. The same bar the
    Over/Under tables use, so two tables in one run's output can be read against each other."""
    probs = np.tile([0.6, 0.3, 0.1], (5, 1))
    metrics, lines = multiclass_calibration(np.zeros(5, dtype=int), probs, probs, CLASSES)
    assert not any(key.startswith("reliability_") for key in metrics)
    assert any("no bucket reaches the reporting minimum" in line for line in lines)


def _test_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "fixture_id": ["f1", "f2"],
            "game_date": ["2026-05-01", "2026-05-02"],
            "season": [2025, 2025],
            "LEAGUE": ["epl", "csl"],
            "label": [0, 2],
            "home_goals": [2, 0],
            "away_goals": [1, 3],
            "home_corners": [7.0, None],
            "away_corners": [4.0, None],
            "xg_for_home": [1.4, None],
            "xg_against_home": [1.1, None],
            "xg_for_away": [1.2, None],
            "xg_against_away": [1.3, None],
            "form_pts_home": [2.0, 1.0],
        }
    )


FEATURE_COLS = ["xg_for_home", "xg_against_home", "xg_for_away", "xg_against_away", "form_pts_home"]


def test_saved_rows_carry_the_availability_flags_the_cuts_need():
    """The point of saving rows rather than a headline: 'does the model do worse where xG is
    missing' becomes a groupby instead of a re-run. Season 2021 carries NO xG in any of the 18
    leagues and 2022 only 57%, so the training split is ~49% covered against a test split at
    98% -- a train/test mismatch in feature AVAILABILITY that no pooled metric can show."""
    probs = np.array([[0.6, 0.2, 0.2], [0.2, 0.2, 0.6]])
    rows = build_test_prediction_rows(
        _test_frame(),
        FEATURE_COLS,
        probs,
        probs,
        np.array([1.5, 0.9]),
        np.array([1.0, 1.8]),
        CLASSES,
        "football_xgb_vTEST",
    )
    assert list(rows["has_xg_features"]) == [True, False]
    assert list(rows["has_corners"]) == [True, False]
    assert rows["feature_completeness"].tolist() == [1.0, 0.2]


def test_saved_rows_record_the_verdict_and_stay_joinable():
    """Keyed on fixture_id and stamped with the artefact version, because the comparison this
    enables is a join between two runs -- the caveat 'corners MAE 2.167 -> 2.157 is not an
    improvement, the test set grew 35%' is only answerable over the shared subset."""
    probs = np.array([[0.6, 0.2, 0.2], [0.2, 0.2, 0.6]])
    rows = build_test_prediction_rows(
        _test_frame(),
        FEATURE_COLS,
        probs,
        probs,
        np.array([1.5, 0.9]),
        np.array([1.0, 1.8]),
        CLASSES,
        "football_xgb_vTEST",
    )
    assert list(rows["correct"]) == [True, True]
    assert set(rows["model_version"]) == {"football_xgb_vTEST"}
    assert list(rows["fixture_id"]) == ["f1", "f2"]
    for cls in CLASSES:
        assert f"p_{cls}" in rows and f"p_{cls}_uncalibrated" in rows


def test_the_xg_availability_columns_are_real_feature_names():
    """A typo here would silently mark every fixture as having xG. Pinned against the model's
    own feature list rather than restated."""
    from app.models_ml.football_features import FEATURE_NAMES

    assert set(XG_FEATURE_COLUMNS) <= set(FEATURE_NAMES)
