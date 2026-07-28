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
