from abc import ABC, abstractmethod

from app.models_ml.schemas import ModelMetrics, PredictionResult


class BaseModel(ABC):
    """Shared interface so the inference engine (ModelRunner) is model-agnostic (TDD §3.1,
    §3.4). Sport-specific subclasses implement the concrete feature handling."""

    def __init__(self, artefact_path: str, version: str | None = None) -> None:
        self.artefact_path = artefact_path
        # The models_registry.version string (e.g. "nba_xgb_v20260728124247"), distinct from
        # artefact_path (a filesystem path) — Prediction.model_version should record this,
        # not the path, for a portable, human-readable audit trail.
        self.version = version or artefact_path

    @abstractmethod
    def predict(self, features: dict) -> PredictionResult: ...

    @abstractmethod
    def calibrate(self, val_features: list[dict], val_outcomes: list[str]) -> None: ...

    @abstractmethod
    def evaluate(self, test_features: list[dict], test_outcomes: list[str]) -> ModelMetrics: ...
