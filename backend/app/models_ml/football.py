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

    xg_home/xg_away and corners_xg_home/away ALSO go through their own isotonic regression
    calibration (mapping raw predicted rate -> empirically-observed real rate on validation
    data) before being returned — added after a real, found-in-production issue: Over/Under
    goals/corners (app/models_ml/markets.py) had NO calibration at all, just a raw Poisson CDF
    on Layer 1's uncalibrated output, and for fixtures whose feature distribution the training
    data barely covers (Scottish Premiership/MLS/CSL, sharing this EPL/Brasileirão-trained
    model per CLAUDE.md), that produced implausibly extreme Over/Under probabilities (99-100%)
    even once real, populated TeamFeatures ruled out stale/missing data as the cause. Layer 2
    (the 1X2 classifier) deliberately keeps consuming the RAW, uncalibrated xG — this
    correction only affects the value returned for the Over/Under markets.

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
        "h2h_avg_goals_scored_home",
        "h2h_avg_goals_allowed_home",
        "key_players_available_home",
        "key_players_available_away",
        "key_players_per_combined_home",
        "key_players_per_combined_away",
        "moneyline_implied_prob_home",
        "elo_diff",
        "win_streak_home",
        "win_streak_away",
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
        xg_home_raw = max(0.0, float(layer1_home_model.predict(layer1_row)[0]))
        xg_away_raw = max(0.0, float(layer1_away_model.predict(layer1_row)[0]))

        # Layer 2 must keep consuming the RAW, uncalibrated xG — that's what
        # train_football.py's add_xg() fed it during training (calibration is fit
        # afterward, on top of Layer 1's own predictions). Feeding Layer 2 the calibrated
        # value here would be a real train/serve mismatch, not an improvement.
        layer2_features = {"xg_home": xg_home_raw, "xg_away": xg_away_raw, **features}
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

        # Corners-Poisson-regressors (Over/Under corners market, see app/models_ml/markets.py)
        # — a separate pair of regressors bundled in the SAME artefact. Originally reused
        # Layer 1's exact feature vector (a documented simplification); now trained on that
        # same 21 PLUS 4 corners-specific rolling features (CORNERS_FEATURE_NAMES, see
        # app/models_ml/football_features.py) — corners_row is only the same as layer1_row for
        # an OLDER artefact that has no "corners_feature_names" key at all (trained before this
        # existed, so its corners_home_model/corners_away_model only ever saw the 21-feature
        # vector — reusing layer1_row for those is correct, not a fallback shortcut). Older
        # artefacts entirely missing corners_home_model/corners_away_model still work too —
        # .get() keeps predict() working against them, with corners_xg_* left None (never
        # fabricated).
        corners_home_model = artefact.get("corners_home_model")
        corners_away_model = artefact.get("corners_away_model")
        corners_feature_names = artefact.get("corners_feature_names")
        corners_row = (
            self._to_row(features, tuple(corners_feature_names))
            if corners_feature_names is not None
            else layer1_row
        )
        corners_xg_home_raw = (
            max(0.0, float(corners_home_model.predict(corners_row)[0]))
            if corners_home_model is not None
            else None
        )
        corners_xg_away_raw = (
            max(0.0, float(corners_away_model.predict(corners_row)[0]))
            if corners_away_model is not None
            else None
        )

        # Regression calibration for the Over/Under markets (app/models_ml/markets.py) — a
        # real, found-in-production issue, not a theoretical one: Layer 1/corners regressors
        # can produce implausibly low expected-goals/corners totals for fixtures whose feature
        # distribution the training data (EPL/Brasileirão only) barely covers — e.g. Scottish
        # Premiership/MLS/CSL fixtures reusing this same model per CLAUDE.md's documented
        # scope decision. Unlike the 1X2 classifier's probability calibrators above, these map
        # a raw PREDICTED RATE to the empirically-observed actual rate on real validation-set
        # fixtures (still IsotonicRegression — the same tool works for calibrating a monotonic
        # regression output, not just a classifier's probability) — y_min=0.0 since goals/
        # corners can't be negative, no y_max (unlike the [0.001, 0.999] probability bound).
        # Only used for the FINAL xg_home/xg_away/corners_xg_* returned here — Layer 2 above
        # deliberately keeps consuming the raw, uncalibrated value it was trained against.
        # .get() keeps this working (uncalibrated) against an older artefact with no
        # calibrator keys at all.
        xg_home_calibrator = artefact.get("xg_home_calibrator")
        xg_away_calibrator = artefact.get("xg_away_calibrator")
        corners_home_calibrator = artefact.get("corners_home_calibrator")
        corners_away_calibrator = artefact.get("corners_away_calibrator")

        xg_home = (
            max(0.0, float(xg_home_calibrator.predict([xg_home_raw])[0]))
            if xg_home_calibrator is not None
            else xg_home_raw
        )
        xg_away = (
            max(0.0, float(xg_away_calibrator.predict([xg_away_raw])[0]))
            if xg_away_calibrator is not None
            else xg_away_raw
        )
        corners_xg_home = (
            max(0.0, float(corners_home_calibrator.predict([corners_xg_home_raw])[0]))
            if corners_xg_home_raw is not None and corners_home_calibrator is not None
            else corners_xg_home_raw
        )
        corners_xg_away = (
            max(0.0, float(corners_away_calibrator.predict([corners_xg_away_raw])[0]))
            if corners_xg_away_raw is not None and corners_away_calibrator is not None
            else corners_xg_away_raw
        )

        return PredictionResult(
            home_prob=float(probs["home"]),
            draw_prob=float(probs["draw"]),
            away_prob=float(probs["away"]),
            xg_home=xg_home,
            xg_away=xg_away,
            corners_xg_home=corners_xg_home,
            corners_xg_away=corners_xg_away,
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
