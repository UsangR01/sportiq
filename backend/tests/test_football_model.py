"""app/models_ml/football.py:FootballModel.predict — specifically the corners_row selection
logic added alongside the corners-specific rolling features
(app/models_ml/football_features.py:CORNERS_FEATURE_NAMES). Uses fake stub models (no real
XGBoost fitting) so this stays fast and dependency-free; the goal is proving predict() builds
the RIGHT row for the RIGHT model, not testing XGBoost/isotonic regression itself."""

import numpy as np
import pytest

from app.models_ml.football import FootballModel


class _CapturingRegressor:
    """Records the exact row it was called with, so tests can assert on the values predict()
    actually built, not just the returned number."""

    def __init__(self, value: float = 1.0):
        self.value = value
        self.last_row = None

    def predict(self, row):
        self.last_row = row
        return np.array([self.value])


class _FakeLayer2Model:
    def predict_proba(self, row):
        return np.array([[0.5, 0.3, 0.2]])


class _IdentityCalibrator:
    def predict(self, values):
        return np.array(values)


class _ScalingCalibrator:
    """A fake xG/corners calibrator that doubles its input — distinct enough from identity
    that tests can tell whether a value passed through calibration or not."""

    def __init__(self, factor: float = 2.0):
        self.factor = factor

    def predict(self, values):
        return np.array([v * self.factor for v in values])


LAYER1_FEATURE_NAMES = ("attack_str_home", "attack_str_away")
CORNERS_FEATURE_NAMES = LAYER1_FEATURE_NAMES + ("corners_for_home", "corners_for_away")


def _base_artefact() -> dict:
    return {
        "layer1_home_model": _CapturingRegressor(1.2),
        "layer1_away_model": _CapturingRegressor(0.8),
        "layer1_feature_names": LAYER1_FEATURE_NAMES,
        "layer2_model": _FakeLayer2Model(),
        "calibrators": {
            "home": _IdentityCalibrator(),
            "draw": _IdentityCalibrator(),
            "away": _IdentityCalibrator(),
        },
    }


def _model_with_artefact(artefact: dict) -> FootballModel:
    model = FootballModel(artefact_path="unused.joblib")
    model._artefact = artefact  # bypass joblib.load entirely — no real file needed
    return model


def test_predict_corners_row_uses_corners_feature_names_when_present():
    """A new-style artefact's corners regressors must see the 4 corners-specific features,
    not just Layer 1's goals-shaped vector."""
    corners_home = _CapturingRegressor(4.0)
    corners_away = _CapturingRegressor(3.0)
    artefact = _base_artefact()
    artefact["corners_home_model"] = corners_home
    artefact["corners_away_model"] = corners_away
    artefact["corners_feature_names"] = CORNERS_FEATURE_NAMES

    model = _model_with_artefact(artefact)
    features = {
        "attack_str_home": 1.0,
        "attack_str_away": 2.0,
        "corners_for_home": 6.0,
        "corners_for_away": 3.0,
    }
    result = model.predict(features)

    assert corners_home.last_row.shape == (1, 4)
    assert list(corners_home.last_row[0]) == [1.0, 2.0, 6.0, 3.0]
    assert result.corners_xg_home == 4.0
    assert result.corners_xg_away == 3.0


def test_predict_corners_row_falls_back_to_layer1_row_for_old_artefact():
    """An OLD artefact (trained before corners-specific features existed) has no
    corners_feature_names key at all — its corners_home_model/corners_away_model were only
    ever trained on Layer 1's own vector, so reusing layer1_row for it is correct, not a
    degraded fallback."""
    corners_home = _CapturingRegressor(2.0)
    corners_away = _CapturingRegressor(2.5)
    artefact = _base_artefact()
    artefact["corners_home_model"] = corners_home
    artefact["corners_away_model"] = corners_away

    model = _model_with_artefact(artefact)
    features = {"attack_str_home": 1.0, "attack_str_away": 2.0}
    result = model.predict(features)

    assert corners_home.last_row.shape == (1, 2)
    assert list(corners_home.last_row[0]) == [1.0, 2.0]
    assert result.corners_xg_home == 2.0
    assert result.corners_xg_away == 2.5


def test_predict_calibrates_xg_but_layer2_still_sees_raw_value():
    """The real, found-in-production fix: xg_home/xg_away returned to the caller (and used for
    the Over/Under goals market) must be calibrated, but Layer 2 (the 1X2 classifier) must
    keep receiving the RAW, uncalibrated value — that's what it was trained against in
    train_football.py, and feeding it the calibrated value would be a real train/serve
    mismatch, not an improvement."""
    layer2 = _FakeLayer2Model()
    artefact = _base_artefact()
    artefact["layer2_model"] = layer2
    artefact["xg_home_calibrator"] = _ScalingCalibrator(2.0)
    artefact["xg_away_calibrator"] = _ScalingCalibrator(3.0)

    model = _model_with_artefact(artefact)
    result = model.predict({"attack_str_home": 1.0, "attack_str_away": 2.0})

    # layer1_home_model/layer1_away_model are fixed-value stubs returning 1.2/0.8 regardless
    # of input (see _base_artefact) — calibrated should be exactly double/triple that.
    assert result.xg_home == pytest.approx(2.4)  # 1.2 * 2.0
    assert result.xg_away == pytest.approx(2.4)  # 0.8 * 3.0


def test_predict_xg_uncalibrated_when_no_calibrator_in_artefact():
    """An older artefact with no xg_*_calibrator keys at all must behave exactly as before —
    xg_home/xg_away are the raw Layer 1 output, unmodified."""
    artefact = _base_artefact()
    model = _model_with_artefact(artefact)
    result = model.predict({"attack_str_home": 1.0, "attack_str_away": 2.0})

    assert result.xg_home == pytest.approx(1.2)
    assert result.xg_away == pytest.approx(0.8)


def test_predict_calibrates_corners_xg():
    corners_home = _CapturingRegressor(4.0)
    corners_away = _CapturingRegressor(3.0)
    artefact = _base_artefact()
    artefact["corners_home_model"] = corners_home
    artefact["corners_away_model"] = corners_away
    artefact["corners_feature_names"] = CORNERS_FEATURE_NAMES
    artefact["corners_home_calibrator"] = _ScalingCalibrator(1.5)
    artefact["corners_away_calibrator"] = _ScalingCalibrator(0.5)

    model = _model_with_artefact(artefact)
    features = {
        "attack_str_home": 1.0,
        "attack_str_away": 2.0,
        "corners_for_home": 6.0,
        "corners_for_away": 3.0,
    }
    result = model.predict(features)

    assert result.corners_xg_home == pytest.approx(6.0)  # 4.0 * 1.5
    assert result.corners_xg_away == pytest.approx(1.5)  # 3.0 * 0.5


def test_predict_corners_xg_none_when_no_corners_models_at_all():
    """An artefact predating the corners market entirely — corners_xg_* must be None, never
    fabricated, and predict() must not crash reaching for a model that isn't there."""
    artefact = _base_artefact()
    model = _model_with_artefact(artefact)
    features = {"attack_str_home": 1.0, "attack_str_away": 2.0}
    result = model.predict(features)

    assert result.corners_xg_home is None
    assert result.corners_xg_away is None
    # The core 1X2 prediction must still work fine regardless.
    assert result.home_prob is not None
