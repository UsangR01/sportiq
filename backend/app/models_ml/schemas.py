from dataclasses import dataclass


@dataclass(frozen=True)
class PredictionResult:
    home_prob: float
    away_prob: float
    draw_prob: float | None = None  # absent for two-outcome sports (e.g. NBA)
    # Football only — Layer 1's own expected-goals output and the corners-Poisson-regressors'
    # output (app/models_ml/football.py), both None for NBA. Feed app/models_ml/markets.py's
    # Over/Under probability calculation; not used by the core home/draw/away prediction itself.
    xg_home: float | None = None
    xg_away: float | None = None
    corners_xg_home: float | None = None
    corners_xg_away: float | None = None


@dataclass(frozen=True)
class ModelMetrics:
    rps_score: float
    brier_score: float
    accuracy: float
    flat_stake_roi: float
