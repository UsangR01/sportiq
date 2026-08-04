from abc import ABC, abstractmethod
from pathlib import Path

from app.core.config import get_settings
from app.models_ml.schemas import ModelMetrics, PredictionResult


def resolve_artefact_path(stored: str) -> str:
    """Turn whatever models_registry holds into a path that exists on THIS machine.

    Registry rows should hold a bare filename, resolved against settings.models_path. They did
    not always: every model registered before this stored an absolute Windows path
    (C:\\Users\\...\\ml\\artifacts\\football_xgb_*.joblib), which cannot load in a Linux
    container — so model promotion, which the TDD deliberately makes a DB update rather than a
    redeploy, would have failed at the first deploy.

    Two fallbacks, in order:
      1. The stored value resolves to a real file -> use it. Keeps pre-existing absolute rows
         working on the machine that wrote them, so this is not a flag-day migration.
      2. Otherwise take the FILENAME and look in models_path. The filename has to be extracted
         by hand rather than with Path().name because a Windows path read on Linux has no
         separators posixpath recognises: Path("C:\\a\\b.joblib").name is the whole string
         there, which would then be looked up as a filename and silently miss.
    """
    direct = Path(stored)
    if direct.is_file():
        return str(direct)
    name = stored.replace("\\", "/").rsplit("/", 1)[-1]
    return str(get_settings().models_path / name)


class BaseModel(ABC):
    """Shared interface so the inference engine (ModelRunner) is model-agnostic (TDD §3.1,
    §3.4). Sport-specific subclasses implement the concrete feature handling."""

    def __init__(self, artefact_path: str, version: str | None = None) -> None:
        self.artefact_path = resolve_artefact_path(artefact_path)
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
