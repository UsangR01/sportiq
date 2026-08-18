"""The market-implied features: devig math, train/serve parity, and the adoption's terms.

Adopted 2026-08-18 for the 1X2 market (RPS 0.2095 -> 0.2063, accuracy 0.5088 -> 0.5103 on the
held-out test season, arms ...195635/...200900) after FAILING a goals-scoped bar — the full
history and the pre-registered live revocation condition live in football_features.py's
FEATURE_NAMES block. These tests pin what must not drift.
"""

import pytest

from app.models_ml.football_features import (
    FEATURE_NAMES,
    assemble_from_game_log,
    devig_1x2,
    devig_over,
)


def test_devig_removes_the_overround():
    # A 5% overround book on an even match: raw inverses sum to ~1.05, devigged sums to 1.
    probs = devig_1x2(2.85, 3.1, 2.85)
    assert probs is not None
    assert sum(probs) == pytest.approx(1.0)
    assert probs[0] == pytest.approx(probs[2])  # symmetric prices, symmetric probabilities
    assert probs[0] > 1 / 2.85 / 1.10  # sanity: same order of magnitude as the raw inverse


def test_devig_refuses_an_incomplete_or_nonsense_market():
    """Two legs of a three-way market cannot be normalised into probabilities, and a price at
    or below 1.0 is not a price. None, not a guess — the feature goes missing instead."""
    assert devig_1x2(2.0, None, 3.0) is None
    assert devig_1x2(2.0, 1.0, 3.0) is None
    assert devig_1x2(0.0, 3.0, 3.0) is None
    assert devig_over(1.9, None) is None
    assert devig_over(1.0, 1.9) is None


def test_devig_over_matches_the_training_side_math():
    """collect_football_data_co_uk_odds.py devigs the historical prices with the same formula;
    if the two ever diverge the feature means different things at train and serve time."""
    p_over = devig_over(1.90, 1.90)
    assert p_over == pytest.approx(0.5)
    p_over = devig_over(1.50, 2.50)
    assert p_over == pytest.approx((1 / 1.5) / (1 / 1.5 + 1 / 2.5))


def test_market_features_are_in_the_vector_and_flow_through_assembly():
    assert FEATURE_NAMES[-3:] == (
        "market_implied_home",
        "market_implied_away",
        "market_implied_over25",
    )
    import pandas as pd

    log = pd.DataFrame(
        [
            {
                "FIXTURE_ID": "1",
                "TEAM_ID": "A",
                "OPPONENT_ID": "B",
                "HOME_AWAY": "home",
                "GF": 1,
                "GA": 0,
                "WDL": "W",
                "GAME_DATE": pd.Timestamp("2026-01-01"),
            },
            {
                "FIXTURE_ID": "1",
                "TEAM_ID": "B",
                "OPPONENT_ID": "A",
                "HOME_AWAY": "away",
                "GF": 0,
                "GA": 1,
                "WDL": "L",
                "GAME_DATE": pd.Timestamp("2026-01-01"),
            },
        ]
    )
    as_of = pd.Timestamp("2026-02-01")

    features = assemble_from_game_log(
        log,
        as_of,
        "A",
        "B",
        market_implied_home=0.51,
        market_implied_away=0.22,
        market_implied_over25=0.44,
    )
    assert features["market_implied_home"] == 0.51
    assert features["market_implied_away"] == 0.22
    assert features["market_implied_over25"] == 0.44
    # And a caller passing nothing gets None, never a fabricated neutral value.
    bare = assemble_from_game_log(log, as_of, "A", "B")
    assert bare["market_implied_home"] is None
