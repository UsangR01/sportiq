"""Computing and reading per-fixture driver explanations.

Two halves, deliberately in one module because they have to agree about the stored shape:

* the WRITE half runs at prediction time (`compute_driver_contributions`), because exact
  contributions are a function of the feature vector as it stood at that instant and cannot be
  recovered afterwards;
* the READ half turns stored contributions into display rows (`explain_pick`), and is where the
  divergence guard lives.

See `app/models_ml/attribution.py` for why football is explained by a market-blind variant while
tennis and NBA/WNBA explain themselves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import joblib
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models_ml.attribution import (
    GroupContribution,
    NotExplainable,
    contributions_for_selection,
    contributions_for_single_estimator,
    group_contributions,
    raw_contributions,
)
from app.models_ml.base import resolve_artefact_path
from app.models_ml.football import FootballModel
from app.predictions.models import ModelRegistry
from app.sports.models import Sport

logger = logging.getLogger(__name__)

#: A market-blind artefact's registry version always ends here, because `--market-blind` puts the
#: variant in the FILENAME and `model_version_for()` derives the version string from it. The
#: suffix is how a blind row is found; the artefact's own `market_blind` key is then asserted, so
#: a mislabelled file cannot quietly become the explanation source.
BLIND_VERSION_SUFFIX = "_blind"

#: Sports whose SERVING model is already market-blind in practice, measured rather than assumed:
#: `moneyline_implied_prob_home` is used in 0 splits by either, so a second artefact would be
#: pure cost. Football spends 19-27% of its splits on market features and is not in this set.
SELF_EXPLAINING_SPORTS = frozenset({"tennis", "nba"})

#: How many factor rows the fixture panel shows (design spec §3.2).
PANEL_ROWS = 3

_blind_model_cache: dict[str, FootballModel | None] = {}


@dataclass(frozen=True, slots=True)
class PickExplanation:
    """What the expanded panel renders, or nothing at all."""

    rows: list[GroupContribution]
    #: The version that produced the CONTRIBUTIONS -- not necessarily the one that produced the
    #: probability on the card. Surfaced so a support question can be answered exactly.
    explained_by: str
    #: True when the explanation comes from a different model than the displayed probability, in
    #: which case the UI must not present the rows as summing to that probability.
    is_market_blind: bool


async def _load_blind_model(db: AsyncSession, sport_slug: str) -> FootballModel | None:
    """The newest registered market-blind artefact for a sport, or None if none is staged."""
    if sport_slug in _blind_model_cache:
        return _blind_model_cache[sport_slug]

    row = (
        await db.execute(
            select(ModelRegistry.version, ModelRegistry.artefact_path)
            .join(Sport, Sport.id == ModelRegistry.sport_id)
            .where(Sport.slug == sport_slug, ModelRegistry.version.endswith(BLIND_VERSION_SUFFIX))
            .order_by(ModelRegistry.trained_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        # Not an error: a sport with no blind artefact simply carries no explanation, and the
        # panel is suppressed. Logged once per process because the cache stops it repeating.
        logger.info("no market-blind artefact registered for %s; explanations disabled", sport_slug)
        _blind_model_cache[sport_slug] = None
        return None

    version, artefact_path = row
    model = FootballModel(artefact_path, version)
    bundle = joblib.load(resolve_artefact_path(artefact_path))
    if not bundle.get("market_blind"):
        # The suffix said blind and the artefact says otherwise. Refuse rather than explain a
        # pick using the bookmaker's own price dressed up as football reasoning.
        logger.error("artefact %s is suffixed _blind but was trained market-aware", version)
        _blind_model_cache[sport_slug] = None
        return None

    _blind_model_cache[sport_slug] = model
    return model


def _football_contributions(model: FootballModel, features: dict) -> dict:
    """Per-estimator TreeSHAP for the four count regressors that drive every football market."""
    bundle = model._artefact  # noqa: SLF001 - same package, and loading it twice is wasteful
    layer1_names = list(bundle["layer1_feature_names"])
    corners_names = list(bundle.get("corners_feature_names") or [])

    estimators: dict[str, dict[str, float]] = {
        "layer1_home": raw_contributions(bundle["layer1_home_model"], features, layer1_names),
        "layer1_away": raw_contributions(bundle["layer1_away_model"], features, layer1_names),
    }
    if corners_names:
        estimators["corners_home"] = raw_contributions(
            bundle["corners_home_model"], features, corners_names
        )
        estimators["corners_away"] = raw_contributions(
            bundle["corners_away_model"], features, corners_names
        )
    return estimators


async def compute_driver_contributions(
    db: AsyncSession, sport_slug: str, features: dict, serving_model: object
) -> dict | None:
    """Everything worth storing about WHY, computed at prediction time.

    Returns the JSON blob for `predictions.driver_contributions`, or None when this sport has no
    explainable model staged. Never raises into the prediction path: an explanation is an
    enhancement, and losing one must not cost a fixture its prediction.
    """
    try:
        if sport_slug in SELF_EXPLAINING_SPORTS:
            bundle = joblib.load(resolve_artefact_path(serving_model.artefact_path))
            names = list(bundle["feature_names"])
            return {
                "explained_by": serving_model.version,
                "market_blind": False,
                "self_explaining": True,
                "estimators": {
                    "win": raw_contributions(bundle["model"], features, names),
                },
            }

        blind = await _load_blind_model(db, sport_slug)
        if blind is None:
            return None

        result = blind.predict(features)
        return {
            "explained_by": blind.version,
            "market_blind": True,
            "self_explaining": False,
            "estimators": _football_contributions(blind, features),
            # The blind model's OWN view, kept for two jobs: the divergence guard below, and a
            # non-circular `blind - market` gap for Top calls. Storing it costs four floats and
            # saves re-running a second model on every read.
            "blind_probabilities": {
                "home": result.home_prob,
                "draw": result.draw_prob,
                "away": result.away_prob,
            },
        }
    except Exception:  # noqa: BLE001 - deliberately broad; see the docstring
        logger.exception("driver attribution failed for %s; storing none", sport_slug)
        return None


def _blind_favours(blind_probabilities: dict[str, float | None]) -> str | None:
    """Which 1X2 outcome the market-blind model itself prefers."""
    scored = {key: value for key, value in blind_probabilities.items() if value is not None}
    return max(scored, key=scored.__getitem__) if scored else None


#: Selections whose direction the divergence guard can meaningfully test against a 1X2 favourite.
#: An Over/Under pick is about how MANY goals, not about who wins, so a blind model preferring a
#: different winner says nothing about whether its goals reasoning agrees.
_GUARDED_SELECTIONS = {"home": "home", "away": "away", "1X": "home", "X2": "away"}


def explain_pick(stored: dict | None, *, market: str, selection: str) -> PickExplanation | None:
    """Turn stored contributions into display rows, or None if the panel must be suppressed.

    Suppression is the right outcome in several ordinary situations -- an old prediction, a draw
    pick, a market with no route -- and in one that matters more:

    THE DIVERGENCE GUARD. When the market-blind model favours a DIFFERENT outcome from the pick
    on the card, its contributions are reasons to make the OPPOSITE call. Rendering them would
    dress up a disagreement as a justification. The rate is worth watching in its own right: a
    rising one means the market is carrying more of the pick than the football is.
    """
    if not stored:
        return None

    estimators = stored.get("estimators") or {}
    try:
        if stored.get("self_explaining"):
            combined = contributions_for_single_estimator(
                estimators.get("win", {}), selection=selection
            )
        else:
            blind_probabilities = stored.get("blind_probabilities") or {}
            guarded = _GUARDED_SELECTIONS.get(selection)
            if guarded is not None:
                favoured = _blind_favours(blind_probabilities)
                if favoured is not None and favoured != guarded:
                    logger.info(
                        "divergence: blind model favours %s, card shows %s - panel suppressed",
                        favoured,
                        selection,
                    )
                    return None
            combined = contributions_for_selection(estimators, market=market, selection=selection)

        rows = group_contributions(combined, top_n=PANEL_ROWS)
    except NotExplainable as exc:
        logger.debug("no explanation for %s/%s: %s", market, selection, exc)
        return None

    if not rows:
        return None
    return PickExplanation(
        rows=rows,
        explained_by=stored.get("explained_by", "unknown"),
        is_market_blind=bool(stored.get("market_blind")),
    )
