"""Pure, DB-free helpers for the /picks algorithm (TDD §4.2), kept separate from router.py
so the core math is unit-testable without a database.

Generalised beyond the original h2h-only (home/draw/away) market to also cover double chance
(1X/X2) and Over/Under goals/corners — see app/models_ml/markets.py for the probability side
of double chance/totals; this module stays focused on odds lookup and outcome selection, which
is identical in shape across all four markets (2 or 3 candidates, pick the model's favourite
among whichever have real odds)."""

from dataclasses import dataclass


def compute_expected_value(probability: float, odds: float) -> float:
    """EV = (prob × (odds − 1)) − (1 − prob), per TDD §3.6 / §4.2 step 5."""
    return probability * (odds - 1) - (1 - probability)


@dataclass(frozen=True)
class OutcomeCandidate:
    selection: str  # "home" | "draw" | "away" | "1X" | "X2" | "over" | "under"
    probability: float
    odds: float


def best_outcome_from_candidates(candidates: list[OutcomeCandidate]) -> OutcomeCandidate | None:
    """Shared selection logic: the highest-probability candidate among whichever have BOTH a
    real model probability and real odds. Used by every market — h2h passes 3 candidates,
    double_chance/totals pass 2."""
    real = [c for c in candidates if c.probability is not None and c.odds is not None]
    if not real:
        return None
    return max(real, key=lambda c: c.probability)


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
    return best_outcome_from_candidates(
        [
            OutcomeCandidate("home", home_prob, home_odds),
            OutcomeCandidate("draw", draw_prob, draw_odds),
            OutcomeCandidate("away", away_prob, away_odds),
        ]
    )


def best_available_odds(odds_rows: list[dict]) -> dict[str, float | None]:
    """Best (highest) odds per side across all tracked bookmakers for one fixture. Used for
    both h2h (home/draw/away columns) and double_chance (which reuses the same home_odds/
    away_odds columns for its own two outcomes — see app/odds/models.py:Odds)."""
    best = {"home": None, "draw": None, "away": None}
    for row in odds_rows:
        for side in ("home", "draw", "away"):
            value = row.get(f"{side}_odds")
            if value is not None and (best[side] is None or value > best[side]):
                best[side] = value
    return best


def best_totals_odds(odds_rows: list[dict], line: float) -> tuple[float | None, float | None]:
    """Best (highest) over/under odds for one specific totals line (goals or corners),
    across all tracked bookmakers for one fixture. odds_rows must already be filtered to the
    right market (goals_total -> Odds.market == "total", corners_total -> "corners_total")."""
    best_over: float | None = None
    best_under: float | None = None
    for row in odds_rows:
        if row.get("line") != line:
            continue
        over = row.get("over_odds")
        under = row.get("under_odds")
        if over is not None and (best_over is None or over > best_over):
            best_over = over
        if under is not None and (best_under is None or under > best_under):
            best_under = under
    return best_over, best_under
