"""Pure, DB-free helpers for the /picks algorithm (TDD §4.2), kept separate from router.py
so the core math is unit-testable without a database."""

from dataclasses import dataclass


def compute_expected_value(probability: float, odds: float) -> float:
    """EV = (prob × (odds − 1)) − (1 − prob), per TDD §3.6 / §4.2 step 5."""
    return probability * (odds - 1) - (1 - probability)


@dataclass(frozen=True)
class OutcomeCandidate:
    selection: str  # "home" | "draw" | "away"
    probability: float
    odds: float


def best_outcome(
    home_prob: float | None,
    draw_prob: float | None,
    away_prob: float | None,
    home_odds: float | None,
    draw_odds: float | None,
    away_odds: float | None,
) -> OutcomeCandidate | None:
    """The side the model favours most, with its matching odds. Draw is absent for two-outcome
    sports (e.g. NBA), consistent with predictions.draw_prob being nullable."""
    candidates = [
        OutcomeCandidate("home", home_prob, home_odds),
        OutcomeCandidate("draw", draw_prob, draw_odds),
        OutcomeCandidate("away", away_prob, away_odds),
    ]
    candidates = [c for c in candidates if c.probability is not None and c.odds is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.probability)


def best_available_odds(odds_rows: list[dict]) -> dict[str, float | None]:
    """Best (highest) odds per market across all tracked bookmakers for one fixture."""
    best = {"home": None, "draw": None, "away": None}
    for row in odds_rows:
        for market in ("home", "draw", "away"):
            value = row.get(f"{market}_odds")
            if value is not None and (best[market] is None or value > best[market]):
                best[market] = value
    return best


def meets_threshold(best_odds: dict[str, float | None], min_odds: float) -> bool:
    values = [v for v in best_odds.values() if v is not None]
    return bool(values) and max(values) >= min_odds
