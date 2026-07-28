import numpy as np

from app.models_ml.base import BaseModel
from app.models_ml.schemas import ModelMetrics, PredictionResult


class NBAModel(BaseModel):
    """XGBoost binary classifier (objective="binary:logistic") on 12 pre-game features —
    see app/models_ml/nba_features.py for the exact set and why it's 12, not TDD §3.3's 13
    (injury impact and pace differential both omitted, documented there and in CLAUDE.md) —
    followed by isotonic calibration (TDD §3.3). No draw outcome.

    Training (ml/training/train_nba.py) owns the actual model fitting, hyperparameter
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

        # None -> np.nan explicitly (XGBoost's native missing-value handling expects NaN,
        # not None) — mirrors the .astype(float) conversion train_nba.py applies to the same
        # feature columns before fitting.
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
        """Calibration happens in ml/training/train_nba.py (fit on validation predictions,
        bundled into the saved artefact) — promotion is a manual, offline step (TDD
        §3.5/§8.3), not something triggered through the live model instance."""
        raise NotImplementedError("Calibration happens in ml/training/train_nba.py, not here")

    def evaluate(self, test_features: list[dict], test_outcomes: list[str]) -> ModelMetrics:
        """Evaluation happens in ml/training/train_nba.py against the held-out test season —
        see that script's accuracy/brier/ROI reporting, stored on the models_registry row."""
        raise NotImplementedError("Evaluation happens in ml/training/train_nba.py, not here")
