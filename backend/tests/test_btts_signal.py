"""BTTS is measured every training run and deliberately not built — this pins the instrument.

Measured 2026-08-18 on the 2025 test season: pooled r=+0.091, no league of 18 cleared the
admission bar, Brasileirao negative. The market stays out until a run's btts_signal_report says
otherwise, using the SAME thresholds as the goals re-admission trigger, imported so the two
bars cannot drift apart.
"""

import numpy as np
import pandas as pd

from app.models_ml.evaluation import btts_signal_report
from app.predictions.market_signal import MIN_N


def _rows(n: int, informative: bool, league: str = "testleague", seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    xg_home = rng.uniform(0.4, 2.6, n)
    xg_away = rng.uniform(0.3, 2.0, n)
    p_btts = (1 - np.exp(-xg_home)) * (1 - np.exp(-xg_away))
    if informative:
        # Outcomes actually drawn from the model's own probability — as strong as signal gets.
        btts = rng.uniform(size=n) < p_btts
    else:
        # Outcomes independent of the prediction — the base-rate-wearing-a-prediction case.
        btts = rng.uniform(size=n) < 0.54
    home_goals = np.where(btts, 1, rng.integers(0, 2, n))
    away_goals = np.where(btts, 1, 0)
    return pd.DataFrame(
        {
            "league": league,
            "xg_home_pred": xg_home,
            "xg_away_pred": xg_away,
            "home_goals": home_goals,
            "away_goals": away_goals,
        }
    )


def test_real_signal_is_recognised_as_clearing_the_bar():
    metrics, lines = btts_signal_report(_rows(MIN_N + 100, informative=True))
    assert metrics["btts_leagues_clearing_bar"] == 1.0
    assert any("CLEARING THE ADMISSION BAR" in line for line in lines)


def test_no_signal_reports_the_market_stays_unbuilt():
    metrics, lines = btts_signal_report(_rows(MIN_N + 100, informative=False))
    assert metrics["btts_leagues_clearing_bar"] == 0.0
    assert any("stays unbuilt" in line for line in lines)
    assert "btts_signal_r" in metrics  # the pooled figure is always logged, pass or fail


def test_a_small_league_cannot_clear_on_a_lucky_sample():
    """n >= MIN_N is part of the bar: real-looking signal on 60 fixtures is a sample, not a
    market. Informative outcomes on purpose — it must fail on n alone."""
    metrics, _ = btts_signal_report(_rows(60, informative=True))
    assert metrics["btts_leagues_clearing_bar"] == 0.0
