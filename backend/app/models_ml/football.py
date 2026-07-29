import numpy as np

from app.models_ml.base import BaseModel
from app.models_ml.schemas import ModelMetrics, PredictionResult


class FootballModel(BaseModel):
    """Two-layer stack (TDD §3.2): a Poisson-regression expected-goals engine (xgboost,
    objective="count:poisson", one regressor per side) feeds a multiclass 1X2 classifier
    (xgboost objective="multi:softprob" — CatBoost is in requirements.txt per the TDD but the
    two approaches are functionally interchangeable here and XGBoost keeps one boosting
    library across both layers and both sports), followed by one-vs-rest isotonic probability
    calibration per class, renormalised to sum to 1.

    Training (ml/training/train_football.py) owns the actual model fitting, hyperparameter
    search, and calibration; this class only loads the resulting joblib artefact and runs
    inference — mirrors app/models_ml/nba.py's role exactly. See app/models_ml/
    football_features.py for the Layer-1 input feature set and app/workers/run_predictions.py
    for how those features get built."""

    # Layer 2 (1X2 classifier) input = the two Layer-1 xG outputs plus a handful of context
    # signals not already summarized by xG (market price, key-player availability, H2H, form)
    # — everything goal-rate-shaped (attack/defence/win-rate) is deliberately left out of
    # Layer 2's own input since xG already captures that signal; feeding it twice would just
    # be redundant, correlated input to the classifier.
    LAYER2_CONTEXT_FEATURES = (
        "form_pts_home",
        "form_pts_away",
        "h2h_win_rate_home",
        "key_players_available_home",
        "key_players_available_away",
        "key_players_per_combined_home",
        "key_players_per_combined_away",
        "moneyline_implied_prob_home",
    )
    # Fixed order — must match train_football.py's label encoding.
    CLASSES = ("home", "draw", "away")

    def __init__(self, artefact_path: str, version: str | None = None) -> None:
        super().__init__(artefact_path, version)
        self._artefact: dict | None = None  # lazily loaded, cached for the instance lifetime

    def _load_artefact(self) -> dict:
        if self._artefact is None:
            import joblib

            self._artefact = joblib.load(self.artefact_path)
        return self._artefact

    @staticmethod
    def _to_row(features: dict, names: tuple) -> np.ndarray:
        return np.array(
            [[np.nan if features.get(name) is None else float(features[name]) for name in names]],
            dtype=float,
        )

    def predict(self, features: dict) -> PredictionResult:
        artefact = self._load_artefact()
        layer1_home_model = artefact["layer1_home_model"]
        layer1_away_model = artefact["layer1_away_model"]
        layer1_feature_names = artefact["layer1_feature_names"]
        layer2_model = artefact["layer2_model"]
        calibrators = artefact["calibrators"]  # dict: class label -> fitted IsotonicRegression

        layer1_row = self._to_row(features, tuple(layer1_feature_names))
        xg_home = max(0.0, float(layer1_home_model.predict(layer1_row)[0]))
        xg_away = max(0.0, float(layer1_away_model.predict(layer1_row)[0]))

        layer2_features = {"xg_home": xg_home, "xg_away": xg_away, **features}
        layer2_row = self._to_row(
            layer2_features, ("xg_home", "xg_away", *self.LAYER2_CONTEXT_FEATURES)
        )
        raw_probs = layer2_model.predict_proba(layer2_row)[0]  # order matches self.CLASSES

        calibrated = np.array(
            [calibrators[cls].predict([raw_probs[i]])[0] for i, cls in enumerate(self.CLASSES)]
        )
        total = calibrated.sum()
        # A degenerate all-zero calibration output would divide by zero — fall back to the
        # uncalibrated (but still valid, softmax-normalised) raw probabilities rather than
        # propagate a NaN prediction.
        normalized = calibrated / total if total > 0 else raw_probs

        probs = dict(zip(self.CLASSES, normalized, strict=True))
        return PredictionResult(
            home_prob=float(probs["home"]),
            draw_prob=float(probs["draw"]),
            away_prob=float(probs["away"]),
        )

    def calibrate(self, val_features: list[dict], val_outcomes: list[str]) -> None:
        """Calibration happens in ml/training/train_football.py (fit on validation
        predictions, bundled into the saved artefact) — mirrors app/models_ml/nba.py's own
        deferral exactly."""
        raise NotImplementedError("Calibration happens in ml/training/train_football.py, not here")

    def evaluate(self, test_features: list[dict], test_outcomes: list[str]) -> ModelMetrics:
        """Evaluation happens in ml/training/train_football.py against the held-out test
        season — see that script's accuracy/RPS/ROI reporting, stored on the models_registry
        row."""
        raise NotImplementedError("Evaluation happens in ml/training/train_football.py, not here")
