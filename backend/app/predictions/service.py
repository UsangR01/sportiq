from app.predictions.models import ConfidenceTier

# PROVISIONAL: neither the PRD nor TDD defines numeric thresholds for the High/Medium/Low
# confidence tiers shown on fixture cards (PRD DASH-02). Chosen loosely around the TDD §3.2
# benchmark (~55.82% accuracy / 0.1925 RPS for the football classifier) as a starting point —
# revisit once real backtests are available.
HIGH_CONFIDENCE_THRESHOLD = 0.65
MEDIUM_CONFIDENCE_THRESHOLD = 0.55


def confidence_tier_for_probability(probability: float) -> ConfidenceTier:
    if probability >= HIGH_CONFIDENCE_THRESHOLD:
        return ConfidenceTier.HIGH
    if probability >= MEDIUM_CONFIDENCE_THRESHOLD:
        return ConfidenceTier.MEDIUM
    return ConfidenceTier.LOW


def feature_completeness(features: dict) -> float | None:
    """Fraction of `features` carrying a real value (0.0-1.0), or None for an empty vector.

    Deliberately counts every key the caller assembled rather than a per-sport whitelist: each
    sport's feature-assembly function already decides which features exist for it, so anything
    present-but-None here is a genuinely missing input, not an inapplicable one.

    Distinguishes an informed prediction from one the model effectively fell back to the base
    rate for. The real case that motivated it: 26% of retrodicted ATP fixtures came out at
    exactly 0.562, because those players' prior-match history was largely absent — indis-
    tinguishable, in the feed, from a confident 56%. See Prediction.feature_completeness."""
    if not features:
        return None
    return sum(1 for value in features.values() if value is not None) / len(features)
