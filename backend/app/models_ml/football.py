from app.models_ml.base import BaseModel
from app.models_ml.schemas import ModelMetrics, PredictionResult


class FootballModel(BaseModel):
    """Three-layer stack (TDD §3.2): Poisson-regression expected-goals engine (xgboost,
    objective="count:poisson") -> CatBoost 1X2 classifier (XGBoost ensemble fallback) ->
    isotonic probability calibration. Not yet implemented — no trained artefact exists."""

    def predict(self, features: dict) -> PredictionResult:
        raise NotImplementedError("FootballModel has no trained artefact yet")

    def calibrate(self, val_features: list[dict], val_outcomes: list[str]) -> None:
        raise NotImplementedError("FootballModel has no trained artefact yet")

    def evaluate(self, test_features: list[dict], test_outcomes: list[str]) -> ModelMetrics:
        raise NotImplementedError("FootballModel has no trained artefact yet")
