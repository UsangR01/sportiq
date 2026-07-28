from app.models_ml.base import BaseModel
from app.models_ml.schemas import ModelMetrics, PredictionResult


class NBAModel(BaseModel):
    """XGBoost binary classifier (objective="binary:logistic") on 13 pre-game features —
    net rating/pace differentials, rest/back-to-back, home court, last-10 form, H2H,
    salary-weighted injury impact, bookmaker implied probability — followed by isotonic
    calibration (TDD §3.3). No draw outcome. Not yet implemented — no trained artefact."""

    def predict(self, features: dict) -> PredictionResult:
        raise NotImplementedError("NBAModel has no trained artefact yet")

    def calibrate(self, val_features: list[dict], val_outcomes: list[str]) -> None:
        raise NotImplementedError("NBAModel has no trained artefact yet")

    def evaluate(self, test_features: list[dict], test_outcomes: list[str]) -> ModelMetrics:
        raise NotImplementedError("NBAModel has no trained artefact yet")
