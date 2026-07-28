from abc import ABC, abstractmethod

from app.models_ml.schemas import ModelMetrics, PredictionResult


class BaseModel(ABC):
    """Shared interface so the inference engine (ModelRunner) is model-agnostic (TDD §3.1,
    §3.4). Sport-specific subclasses implement the concrete feature handling."""

    def __init__(self, artefact_path: str) -> None:
        self.artefact_path = artefact_path

    @abstractmethod
    def predict(self, features: dict) -> PredictionResult: ...

    @abstractmethod
    def calibrate(self, val_features: list[dict], val_outcomes: list[str]) -> None: ...

    @abstractmethod
    def evaluate(self, test_features: list[dict], test_outcomes: list[str]) -> ModelMetrics: ...
