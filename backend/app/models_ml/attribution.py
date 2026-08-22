"""Per-fixture driver attribution: which real-world factors moved this prediction, and how much.

WHAT THIS IS
------------
XGBoost's own `pred_contribs=True` computes **exact TreeSHAP** -- not an approximation, and with
no `shap` dependency. Contributions plus a bias term reproduce the model's raw margin exactly, so
the numbers genuinely decompose the score rather than gesturing at it. Measured at ~5.4ms per
row, which is nothing beside the API calls a prediction already makes.

WHY IT EXPLAINS A DIFFERENT MODEL FROM THE ONE ON THE CARD
----------------------------------------------------------
`market_implied_*` are real, adopted input features for football -- and among the largest
contributors. A truthful panel built on the SERVING model would therefore keep saying the biggest
reason for a pick is *the bookmaker's price*: honest, circular, and useless next to a screen that
sells "where the model disagrees with the market".

So football explanations come from a parallel **market-blind** artefact
(`train_football.py --market-blind`) that has never seen a price. The consequence has to be
carried through to the UI rather than hidden: the contributions decompose a DIFFERENT model, so
they do NOT sum to the probability on the card and must never be shown as though they do. The
eyebrow says what the DATA says, not why the model called it.

Tennis and NBA/WNBA need no blind variant. Measured 2026-08-22 against the live artefacts:
`moneyline_implied_prob_home` is used in **0 splits** by either, so each serving model is already
effectively market-blind and can explain itself. Football, by contrast, spends 27% of Layer 1's
splits and 19-23% of the corners pair's on the three market features -- which is precisely why
only football needed a second artefact.

WHY 1X2 IS EXPLAINED FROM LAYER 1
---------------------------------
Layer 2 sees ten features, two of which (`xg_home`/`xg_away`) are Layer 1 OUTPUTS. Attribution
against it bottoms out at "expected goals were 1.8 vs 1.1" -- true, and not a reason anyone can
act on. Layer 1's own features are the actionable level, so xG is treated as the bridge rather
than as a driver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - typing only
    import xgboost as xgb

# xgboost IS NOT IMPORTED AT MODULE SCOPE, AND THAT IS LOAD-BEARING RATHER THAN TIDY.
# app/fixtures/router.py imports the read path from here, so anything imported at this level
# lands in the WEB service at startup. Importing xgboost here took app.main from 120MB to 239MB
# and OOM'd the 512MB instance -- the API returned 502 while the only code that needs xgboost
# (raw_contributions) runs exclusively in the worker.

#: Machine feature name -> the label a reader sees. Mapped ONCE, centrally, because the same
#: grouping has to hold for the fixture panel and for Top calls; two copies would drift and there
#: would be no way to tell which one was right.
#:
#: SHAP is additive, so summing raw contributions within a group is valid -- that is the whole
#: reason grouping is permitted here rather than being a presentational fudge.
FEATURE_GROUPS: dict[str, str] = {
    # --- football: Layer 1 (goals, and the 1X2 explanation) and the corners pair -------------
    "attack_str_home": "Goals per game",
    "attack_str_away": "Goals per game",
    "defence_str_home": "Goals per game",
    "defence_str_away": "Goals per game",
    "xg_for_home": "Goals per game",
    "xg_against_home": "Goals per game",
    "xg_for_away": "Goals per game",
    "xg_against_away": "Goals per game",
    "form_pts_home": "Recent form",
    "form_pts_away": "Recent form",
    "win_streak_home": "Recent form",
    "win_streak_away": "Recent form",
    "rest_days_home": "Rest and rotation",
    "rest_days_away": "Rest and rotation",
    "h2h_win_rate_home": "Head-to-head",
    "h2h_avg_goals_scored_home": "Head-to-head",
    "h2h_avg_goals_allowed_home": "Head-to-head",
    "elo_diff": "Overall strength",
    "league_avg_goals": "League norms",
    "league_home_win_rate": "League norms",
    "corners_for_home": "Corners won and conceded",
    "corners_against_home": "Corners won and conceded",
    "corners_for_away": "Corners won and conceded",
    "corners_against_away": "Corners won and conceded",
    # --- tennis ------------------------------------------------------------------------------
    "rank_diff": "Ranking gap",
    "form_win_rate_home": "Recent form",
    "form_win_rate_away": "Recent form",
    "days_since_last_match_home": "Rest and rotation",
    "days_since_last_match_away": "Rest and rotation",
    "h2h_win_rate_surface_home": "Head-to-head",
    "surface_win_rate_home": "This surface",
    "surface_win_rate_away": "This surface",
    "surface_streak_home": "This surface",
    "surface_streak_away": "This surface",
    # --- NBA / WNBA --------------------------------------------------------------------------
    "back_to_back_home": "Rest and rotation",
    "back_to_back_away": "Rest and rotation",
    "last10_win_rate_home": "Recent form",
    "last10_win_rate_away": "Recent form",
    "last10_point_diff_home": "Recent form",
    "last10_point_diff_away": "Recent form",
    "net_rating_diff": "Overall strength",
    "home_court_indicator": "Home advantage",
    "key_players_available_home": "Key players available",
    "key_players_available_away": "Key players available",
    "key_players_per_combined_home": "Key players available",
    "key_players_per_combined_away": "Key players available",
    # `win_streak_home`/`win_streak_away` and `h2h_win_rate_home` are shared across sports and
    # are already mapped above -- deliberately not repeated.
}

#: Features that deliberately carry no group, so that an unmapped name is a real omission rather
#: than something we chose to leave out. Every one is the market's own opinion; see the module
#: docstring for why a price is not a reason.
UNGROUPED_FEATURES: frozenset[str] = frozenset(
    {
        "market_implied_home",
        "market_implied_away",
        "market_implied_over25",
        "moneyline_implied_prob_home",
    }
)


class NotExplainable(Exception):
    """Raised when attribution would be MISLEADING rather than merely unavailable.

    Callers must suppress the panel rather than substitute a fallback: a wrong explanation is
    worse than no explanation, because a reader cannot tell the two apart.
    """


@dataclass(frozen=True, slots=True)
class GroupContribution:
    """One display row: a label, its summed contribution, and its share of the total movement."""

    label: str
    #: Signed, on the model's own margin scale. Positive favours the direction being explained.
    contribution: float
    #: |contribution| as a share of every group's |contribution|. A RELATIVE weight, never a
    #: probability -- these decompose a different model from the one that produced the card.
    weight: float


def raw_contributions(
    model: xgb.XGBModel, row: dict[str, float | None], feature_names: list[str]
) -> dict[str, float]:
    """Exact per-feature TreeSHAP contributions for one fixture, on the model's own margin scale.

    `row` is the same mapping the feature assemblers already build. A missing or None value is
    passed through as NaN, which XGBoost handles natively -- the same treatment it had at training
    time, so an absent feature is explained as absent rather than as a fabricated zero.
    """
    import xgboost as xgb  # noqa: PLC0415 - deliberately deferred; see the module header

    values = np.array(
        [[np.nan if row.get(name) is None else float(row[name]) for name in feature_names]],
        dtype=float,
    )
    matrix = xgb.DMatrix(values, feature_names=feature_names, missing=np.nan)
    contribs = np.asarray(model.get_booster().predict(matrix, pred_contribs=True))
    # (1, n_features + 1) for a single-output model; the final column is the bias.
    flat = contribs.reshape(len(feature_names) + 1)
    return {name: float(flat[index]) for index, name in enumerate(feature_names)}


def group_contributions(
    contributions: dict[str, float], *, top_n: int | None = None
) -> list[GroupContribution]:
    """Sum contributions into display labels, largest absolute movement first.

    Summing within a group is legitimate BECAUSE SHAP is additive -- this is not an average or a
    re-weighting, it is the same exact decomposition read at a coarser grain.
    """
    totals: dict[str, float] = {}
    for name, value in contributions.items():
        if name in UNGROUPED_FEATURES:
            continue
        label = FEATURE_GROUPS.get(name)
        if label is None:
            # Loud rather than silent. A newly added feature that nobody mapped would otherwise
            # vanish from every explanation while still moving every prediction.
            raise NotExplainable(f"feature {name!r} has no display group")
        totals[label] = totals.get(label, 0.0) + value

    magnitude = sum(abs(value) for value in totals.values())
    if magnitude == 0:
        return []
    rows = [
        GroupContribution(label=label, contribution=value, weight=abs(value) / magnitude)
        for label, value in totals.items()
    ]
    rows.sort(key=lambda row_: abs(row_.contribution), reverse=True)
    return rows[:top_n] if top_n is not None else rows


#: How each market's explanation is assembled from the estimators that produce it.
#:
#: The sign convention is the whole contract: **positive always means "this supports the pick on
#: the card"**, whatever the market. Without that, a factor row would read as favourable on a
#: home pick and identical-but-wrong on an away pick, and the copy in the panel could not be
#: written once.
#:
#: - `difference` -- home estimator minus away estimator. Both Poisson regressors use a log link,
#:   so the difference is the contribution to log(home rate / away rate): a genuine "who does
#:   this favour" quantity rather than two numbers put side by side.
#: - `total` -- home plus away, the contribution to the combined count that an Over/Under line is
#:   actually settled against.
#: - `single` -- one estimator whose margin is already the log-odds of a home win.
_ROUTES: dict[str, tuple[str, tuple[str, ...]]] = {
    "h2h": ("difference", ("layer1_home", "layer1_away")),
    "double_chance": ("difference", ("layer1_home", "layer1_away")),
    "goals_total": ("total", ("layer1_home", "layer1_away")),
    "corners_total": ("total", ("corners_home", "corners_away")),
}

#: Selections that lean toward the AWAY side or toward FEWER events, and so need the raw
#: direction flipped before display.
_NEGATED_SELECTIONS: frozenset[str] = frozenset({"away", "X2", "under"})

#: Selections with no expressible direction under a home-minus-away framing.
#:
#: A pure draw is not "less home" or "less away" -- it sits between them, and pretending
#: otherwise would produce a confidently wrong panel. Measured over the whole settled card
#: history, **no draw pick has ever reached a card** (`MIN_EDGE_OVER_BASE_RATE` requires 0.3038
#: and the model gives 92% of fixtures a draw probability inside 0.2-0.3), so this refuses a
#: case that does not arise rather than one users would miss.
_UNDIRECTED_SELECTIONS: frozenset[str] = frozenset({"draw", "X"})


def contributions_for_selection(
    per_estimator: dict[str, dict[str, float]], *, market: str, selection: str
) -> dict[str, float]:
    """Combine per-estimator contributions into one signed set supporting `selection`.

    Raises `NotExplainable` for a market or selection whose direction cannot be expressed, so a
    caller suppresses the panel instead of rendering a plausible-looking wrong one.
    """
    if selection in _UNDIRECTED_SELECTIONS:
        raise NotExplainable(f"selection {selection!r} has no directional explanation")
    route = _ROUTES.get(market)
    if route is None:
        raise NotExplainable(f"market {market!r} has no attribution route")

    mode, estimators = route
    missing = [name for name in estimators if name not in per_estimator]
    if missing:
        raise NotExplainable(f"missing contributions for {', '.join(missing)}")

    home, away = (per_estimator[name] for name in estimators)
    sign = -1.0 if selection in _NEGATED_SELECTIONS else 1.0
    combined: dict[str, float] = {}
    for name in home.keys() | away.keys():
        pair = home.get(name, 0.0), away.get(name, 0.0)
        value = pair[0] - pair[1] if mode == "difference" else pair[0] + pair[1]
        combined[name] = sign * value
    return combined


def contributions_for_single_estimator(
    contributions: dict[str, float], *, selection: str
) -> dict[str, float]:
    """The tennis/NBA case: one binary model whose margin is already the log-odds of a home win.

    No blind variant is needed for either sport -- both were measured using their moneyline
    feature in zero splits -- so this reads the serving model directly.
    """
    if selection in _UNDIRECTED_SELECTIONS:
        raise NotExplainable(f"selection {selection!r} has no directional explanation")
    sign = -1.0 if selection in _NEGATED_SELECTIONS else 1.0
    return {name: sign * value for name, value in contributions.items()}
