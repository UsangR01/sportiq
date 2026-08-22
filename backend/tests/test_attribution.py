"""Attribution engine: exactness, direction, grouping, and refusal.

Every test here fits its own small model rather than loading a registered artefact. That keeps
the suite runnable in CI with no ml/artifacts/ present, and it means a failure points at this
module rather than at whichever model happened to be active that week.
"""

from __future__ import annotations

import numpy as np
import pytest
import xgboost as xgb

from app.models_ml import nba_features, tennis_features
from app.models_ml.attribution import (
    FEATURE_GROUPS,
    UNGROUPED_FEATURES,
    NotExplainable,
    contributions_for_selection,
    contributions_for_single_estimator,
    group_contributions,
    raw_contributions,
)
from app.models_ml.football_features import CORNERS_FEATURE_NAMES, FEATURE_NAMES


def _fitted_model(feature_names: list[str], seed: int = 0) -> xgb.XGBRegressor:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(200, len(feature_names)))
    # A target that genuinely depends on the first two features, so contributions have something
    # real to find rather than fitting noise.
    y = 1.5 * x[:, 0] - 0.8 * x[:, 1] + rng.normal(scale=0.1, size=200)
    model = xgb.XGBRegressor(n_estimators=12, max_depth=3, random_state=seed)
    model.fit(x, y)
    model.get_booster().feature_names = list(feature_names)
    return model


def test_contributions_plus_bias_reproduce_the_raw_margin_exactly() -> None:
    """The property the whole design rests on.

    TreeSHAP here is EXACT, not an approximation, so contributions and the bias must add up to
    the model's own output. If this ever drifts, the panel is decomposing something other than
    the score it claims to explain -- which is worse than showing nothing.
    """
    names = [f"f{i}" for i in range(6)]
    model = _fitted_model(names)
    row = {name: float(index) - 2.5 for index, name in enumerate(names)}

    contributions = raw_contributions(model, row, names)

    values = np.array([[row[name] for name in names]], dtype=float)
    matrix = xgb.DMatrix(values, feature_names=names)
    full = np.asarray(model.get_booster().predict(matrix, pred_contribs=True)).reshape(-1)
    bias = float(full[-1])
    margin = float(model.get_booster().predict(matrix, output_margin=True)[0])

    assert sum(contributions.values()) + bias == pytest.approx(margin, abs=1e-5)


def test_a_missing_feature_is_explained_as_absent_not_as_zero() -> None:
    """None must reach XGBoost as NaN -- its own missing-value path, the same one used in
    training. Substituting 0.0 would explain a fabricated value that was never observed."""
    names = [f"f{i}" for i in range(4)]
    model = _fitted_model(names)

    as_none = raw_contributions(model, {"f0": 1.0, "f1": None, "f2": 0.5, "f3": 0.0}, names)
    as_zero = raw_contributions(model, {"f0": 1.0, "f1": 0.0, "f2": 0.5, "f3": 0.0}, names)

    assert as_none != as_zero


def test_grouping_sums_within_a_label_and_ranks_by_absolute_movement() -> None:
    contributions = {
        "form_pts_home": 0.30,
        "win_streak_home": 0.10,  # same group as form_pts_home -> 0.40 combined
        "elo_diff": -0.60,  # larger in magnitude, so it must rank first
        "rest_days_home": 0.05,
    }

    rows = group_contributions(contributions)

    assert [row.label for row in rows] == ["Overall strength", "Recent form", "Rest and rotation"]
    assert rows[1].contribution == pytest.approx(0.40)
    assert sum(row.weight for row in rows) == pytest.approx(1.0)


def test_market_features_are_dropped_from_every_explanation() -> None:
    """The point of the blind variant. Even handed a market contribution, nothing may surface it."""
    rows = group_contributions({"elo_diff": 0.5, "market_implied_home": 9.0})

    assert [row.label for row in rows] == ["Overall strength"]
    assert rows[0].weight == pytest.approx(1.0)


def test_an_unmapped_feature_raises_rather_than_vanishing() -> None:
    """A new feature that nobody mapped would otherwise keep moving predictions while silently
    disappearing from every explanation -- invisible in exactly the way this project keeps
    getting caught by."""
    with pytest.raises(NotExplainable, match="display group"):
        group_contributions({"some_new_feature": 0.4})


def test_positive_always_means_supports_the_pick_on_the_card() -> None:
    """The sign contract. Home and away picks on the SAME fixture must mirror each other, so the
    panel's copy can be written once instead of per selection."""
    per_estimator = {
        "layer1_home": {"elo_diff": 0.6, "form_pts_home": 0.2},
        "layer1_away": {"elo_diff": -0.1, "form_pts_home": 0.05},
    }

    home = contributions_for_selection(per_estimator, market="h2h", selection="home")
    away = contributions_for_selection(per_estimator, market="h2h", selection="away")

    assert home["elo_diff"] == pytest.approx(0.7)  # 0.6 - (-0.1)
    assert away["elo_diff"] == pytest.approx(-0.7)
    assert all(home[key] == pytest.approx(-away[key]) for key in home)


def test_over_under_combines_the_two_sides_as_a_total_not_a_difference() -> None:
    """An Over/Under line settles against home PLUS away, so a factor that lifts both sides'
    scoring supports OVER -- where the same factor on 1X2 would cancel out."""
    per_estimator = {
        "layer1_home": {"league_avg_goals": 0.4},
        "layer1_away": {"league_avg_goals": 0.4},
    }

    over = contributions_for_selection(per_estimator, market="goals_total", selection="over")
    under = contributions_for_selection(per_estimator, market="goals_total", selection="under")
    h2h = contributions_for_selection(per_estimator, market="h2h", selection="home")

    assert over["league_avg_goals"] == pytest.approx(0.8)
    assert under["league_avg_goals"] == pytest.approx(-0.8)
    assert h2h["league_avg_goals"] == pytest.approx(0.0)


def test_corners_route_to_the_corners_pair_not_to_layer_1() -> None:
    per_estimator = {
        "layer1_home": {"elo_diff": 5.0},
        "layer1_away": {"elo_diff": 0.0},
        "corners_home": {"corners_for_home": 0.3},
        "corners_away": {"corners_for_home": 0.1},
    }

    rows = contributions_for_selection(per_estimator, market="corners_total", selection="over")

    assert rows == {"corners_for_home": pytest.approx(0.4)}
    assert "elo_diff" not in rows


def test_a_draw_pick_is_refused_rather_than_explained_in_a_direction_it_has_not_got() -> None:
    """A draw is not "less home" -- it sits between the two, so a home-minus-away framing cannot
    express it. Measured over the whole settled card history, no draw pick has ever reached a
    card, so this refuses a case that does not arise rather than one users would miss."""
    per_estimator = {"layer1_home": {"elo_diff": 0.1}, "layer1_away": {"elo_diff": 0.2}}

    for selection in ("draw", "X"):
        with pytest.raises(NotExplainable, match="directional"):
            contributions_for_selection(per_estimator, market="h2h", selection=selection)


def test_an_unroutable_market_is_refused() -> None:
    with pytest.raises(NotExplainable, match="attribution route"):
        contributions_for_selection({}, market="btts", selection="yes")


def test_missing_estimator_contributions_are_refused_not_silently_halved() -> None:
    """Explaining a total from only the home side would produce a confident, wrong magnitude."""
    with pytest.raises(NotExplainable, match="layer1_away"):
        contributions_for_selection(
            {"layer1_home": {"elo_diff": 0.5}}, market="goals_total", selection="over"
        )


def test_single_estimator_sports_follow_the_same_sign_contract() -> None:
    home = contributions_for_single_estimator({"rank_diff": 0.9}, selection="home")
    away = contributions_for_single_estimator({"rank_diff": 0.9}, selection="away")

    assert home["rank_diff"] == pytest.approx(0.9)
    assert away["rank_diff"] == pytest.approx(-0.9)


@pytest.mark.parametrize(
    ("sport", "names"),
    [
        ("football layer 1", FEATURE_NAMES),
        ("football corners", CORNERS_FEATURE_NAMES),
        ("tennis", tennis_features.FEATURE_NAMES),
        ("nba", nba_features.FEATURE_NAMES),
    ],
)
def test_every_live_model_feature_has_a_display_group(sport: str, names: tuple[str, ...]) -> None:
    """The guard that keeps this honest as the feature sets move.

    Adding a feature without mapping it should fail HERE, at the point of change, rather than at
    prediction time on one unlucky fixture.
    """
    known = set(FEATURE_GROUPS) | UNGROUPED_FEATURES
    assert [name for name in names if name not in known] == [], sport
