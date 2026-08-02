import numpy as np

from app.models_ml.base import BaseModel
from app.models_ml.schemas import ModelMetrics, PredictionResult


class TennisModel(BaseModel):
    """XGBoost binary classifier (objective="binary:logistic") on the 14 pre-match features
    in app/models_ml/tennis_features.py, followed by isotonic calibration — the same shape as
    app/models_ml/nba.py (2-outcome, no draw), not football's two-layer Poisson xG stack,
    since tennis has no draw either.

    Training (ml/training/train_tennis.py) owns the actual model fitting, hyperparameter
    search, and calibration; this class only loads the resulting joblib artefact (a dict of
    {model, calibrator, feature_names}) and runs inference — see that script for how the
    artefact is produced and app/workers/run_predictions.py for how features get built."""

    def __init__(self, artefact_path: str, version: str | None = None) -> None:
        super().__init__(artefact_path, version)
        self._artefact: dict | None = None  # lazily loaded, cached for the instance lifetime

    def _load_artefact(self) -> dict:
        if self._artefact is None:
            import joblib

            self._artefact = joblib.load(self.artefact_path)
        return self._artefact

    def predict(self, features: dict) -> PredictionResult:
        artefact = self._load_artefact()
        model = artefact["model"]
        calibrator = artefact["calibrator"]
        feature_names = artefact["feature_names"]

        # None -> np.nan explicitly (XGBoost's native missing-value handling expects NaN, not
        # None) — mirrors train_tennis.py's own .astype(float) conversion before fitting.
        row = np.array(
            [
                [
                    np.nan if features.get(name) is None else float(features[name])
                    for name in feature_names
                ]
            ],
            dtype=float,
        )
        raw_prob = float(model.predict_proba(row)[0][1])
        calibrated_prob = float(calibrator.predict([raw_prob])[0])

        return PredictionResult(
            home_prob=calibrated_prob, away_prob=1.0 - calibrated_prob, draw_prob=None
        )

    def calibrate(self, val_features: list[dict], val_outcomes: list[str]) -> None:
        """Calibration happens in ml/training/train_tennis.py (fit on validation predictions,
        bundled into the saved artefact) — promotion is a manual, offline step, not something
        triggered through the live model instance."""
        raise NotImplementedError("Calibration happens in ml/training/train_tennis.py, not here")

    def evaluate(self, test_features: list[dict], test_outcomes: list[str]) -> ModelMetrics:
        """Evaluation happens in ml/training/train_tennis.py against the held-out test
        period — see that script's accuracy/brier/ROI reporting, stored on the
        models_registry row."""
        raise NotImplementedError("Evaluation happens in ml/training/train_tennis.py, not here")
