from dataclasses import dataclass


@dataclass(frozen=True)
class PredictionResult:
    home_prob: float
    away_prob: float
    draw_prob: float | None = None  # absent for two-outcome sports (e.g. NBA)


@dataclass(frozen=True)
class ModelMetrics:
    rps_score: float
    brier_score: float
    accuracy: float
    flat_stake_roi: float
