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


# How far a single book's implied probability may sit from the consensus before its row is
# ignored. Wide enough to keep genuine price disagreement (books differ by a few points, and
# that spread is exactly what "best available" is for), narrow enough to reject a row whose
# sides are the wrong way round.
MAX_IMPLIED_DEVIATION_FROM_CONSENSUS = 0.25


def _consensus_outliers(odds_rows: list[dict]) -> set[int]:
    """Indices of rows whose home/away prices disagree with the consensus badly enough to be
    a data error rather than a keener price.

    Motivating case, from real ATP data: 14 books priced Cameron Norrie at ~1.42 and Ignacio
    Buse at ~2.90, while Polymarket alone had 3.13/1.45 — the same match with the sides
    reversed. Because best odds are the MAXIMUM across books, that single inverted row won
    every time and the card showed the favourite at the underdog's price. Taking the max is
    what makes this dangerous: an outlier is not diluted, it is actively selected for.

    Deliberately keyed on disagreement with the median rather than an absolute bound. An
    inverted price is usually perfectly plausible on its own (3.13 is an ordinary number) and
    only reveals itself against the field, so a fixed ceiling like the training-time
    PLAUSIBLE_MAX_DECIMAL_ODDS would not catch it.
    """
    priced = [
        (i, row["home_odds"])
        for i, row in enumerate(odds_rows)
        if row.get("home_odds") and row.get("away_odds")
    ]
    if len(priced) < 3:
        # Too few books to establish a consensus; trust them all rather than guess.
        return set()
    implied = sorted(1.0 / odds for _, odds in priced)
    mid = len(implied) // 2
    median = implied[mid] if len(implied) % 2 else (implied[mid - 1] + implied[mid]) / 2
    return {
        i for i, odds in priced if abs(1.0 / odds - median) > MAX_IMPLIED_DEVIATION_FROM_CONSENSUS
    }


def best_available_odds(odds_rows: list[dict]) -> dict[str, float | None]:
    """Best (highest) odds per side across all tracked bookmakers for one fixture. Used for
    both h2h (home/draw/away columns) and double_chance (which reuses the same home_odds/
    away_odds columns for its own two outcomes — see app/odds/models.py:Odds).

    Rows that contradict the consensus are excluded first — see _consensus_outliers."""
    outliers = _consensus_outliers(odds_rows)
    best = {"home": None, "draw": None, "away": None}
    for i, row in enumerate(odds_rows):
        if i in outliers:
            continue
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
