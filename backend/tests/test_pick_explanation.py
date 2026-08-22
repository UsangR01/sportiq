"""The read half of attribution: what reaches a card, and — mostly — what does not.

Every test here is about SUPPRESSION, because that is where this feature can do damage. A
missing panel costs a user nothing; a confidently wrong one is a reason to distrust every
number on the screen.
"""

from __future__ import annotations

from app.predictions.explanation import explain_pick

# A football blob shaped exactly as compute_driver_contributions writes it. Home is strongly
# favoured by the blind model, so a home pick is explainable and an away pick is not.
_FOOTBALL = {
    "explained_by": "football_xgb_v20260822000000_blind",
    "market_blind": True,
    "self_explaining": False,
    "estimators": {
        "layer1_home": {"elo_diff": 0.50, "form_pts_home": 0.20, "rest_days_home": 0.05},
        "layer1_away": {"elo_diff": -0.10, "form_pts_home": 0.00, "rest_days_home": 0.00},
        "corners_home": {"corners_for_home": 0.30},
        "corners_away": {"corners_for_home": 0.10},
    },
    "blind_probabilities": {"home": 0.61, "draw": 0.24, "away": 0.15},
}


def test_a_pick_the_blind_model_agrees_with_gets_its_panel() -> None:
    explanation = explain_pick(_FOOTBALL, market="h2h", selection="home")

    assert explanation is not None
    assert explanation.is_market_blind is True
    assert explanation.explained_by.endswith("_blind")
    # Ordered by absolute movement, capped at the three rows the panel renders.
    assert [row.label for row in explanation.rows] == [
        "Overall strength",
        "Recent form",
        "Rest and rotation",
    ]
    assert explanation.rows[0].contribution > 0  # positive == supports the pick shown


def test_the_divergence_guard_suppresses_a_pick_the_blind_model_disagrees_with() -> None:
    """THE GUARD THAT MATTERS. The blind model favours home; the card shows away. Its
    contributions are reasons to make the OPPOSITE call, so rendering them would dress a
    disagreement up as a justification."""
    assert explain_pick(_FOOTBALL, market="h2h", selection="away") is None


def test_double_chance_is_guarded_by_the_side_it_leans_toward() -> None:
    """1X leans home and X2 leans away, so the same guard applies to both — otherwise the
    suppression could be sidestepped by picking the double-chance version of a pick the
    football does not support."""
    assert explain_pick(_FOOTBALL, market="double_chance", selection="1X") is not None
    assert explain_pick(_FOOTBALL, market="double_chance", selection="X2") is None


def test_over_under_is_not_guarded_by_the_1x2_favourite() -> None:
    """An Over/Under pick is about HOW MANY goals, not who wins. A blind model preferring a
    different winner says nothing about whether its goals reasoning agrees, so guarding totals
    on the 1X2 favourite would suppress panels for no reason."""
    for selection in ("over", "under"):
        assert explain_pick(_FOOTBALL, market="goals_total", selection=selection) is not None
        assert explain_pick(_FOOTBALL, market="corners_total", selection=selection) is not None


def test_corners_panel_is_built_from_the_corners_pair() -> None:
    explanation = explain_pick(_FOOTBALL, market="corners_total", selection="over")

    assert explanation is not None
    assert [row.label for row in explanation.rows] == ["Corners won and conceded"]


def test_a_prediction_written_before_attribution_existed_carries_no_panel() -> None:
    """The unbackfillable case. Contributions depend on the feature vector at the instant of
    prediction, so an old row genuinely has none and must show nothing rather than be handed a
    plausible explanation invented after the result was known."""
    assert explain_pick(None, market="h2h", selection="home") is None
    assert explain_pick({}, market="h2h", selection="home") is None


def test_a_draw_pick_is_suppressed_rather_than_given_a_direction() -> None:
    assert explain_pick(_FOOTBALL, market="h2h", selection="draw") is None


def test_a_self_explaining_sport_reads_its_serving_model_directly() -> None:
    """Tennis and NBA use their moneyline feature in zero splits, so the served model is already
    effectively market-blind — no second artefact, and the panel is NOT flagged blind."""
    stored = {
        "explained_by": "tennis_xgb_v20260813023503",
        "market_blind": False,
        "self_explaining": True,
        "estimators": {"win": {"rank_diff": 0.8, "form_win_rate_home": 0.3}},
    }

    home = explain_pick(stored, market="h2h", selection="home")
    away = explain_pick(stored, market="h2h", selection="away")

    assert home is not None and away is not None
    assert home.is_market_blind is False
    assert [row.label for row in home.rows] == ["Ranking gap", "Recent form"]
    # No blind_probabilities, so no divergence guard — and correctly so: the explanation and the
    # served probability come from the SAME model, which cannot disagree with itself.
    assert home.rows[0].contribution == -away.rows[0].contribution


def test_a_market_with_no_route_is_suppressed_not_guessed() -> None:
    assert explain_pick(_FOOTBALL, market="btts", selection="yes") is None
