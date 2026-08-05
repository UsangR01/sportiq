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


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _consensus_outliers(odds_rows: list[dict]) -> set[int]:
    """Indices of rows whose two sides are the wrong way round relative to the field.

    Real case, found by comparing the app against a public odds page: Polymarket consistently
    lists ATP matches with the sides reversed. For Cameron Norrie it showed 3.13/1.45 where 14
    other books had 1.42/2.90; for Khachanov 2.22/1.79 against a consensus of 1.70/2.15.
    Because best odds are the MAXIMUM across books, one reversed row wins outright and the card
    shows the favourite at the underdog's price. Taking the max is what makes an outlier
    dangerous rather than harmless: it is actively selected for, not diluted.

    The test asks whether a row's HOME price looks more like the consensus AWAY price than the
    consensus HOME price — i.e. whether it is inverted — rather than whether it is merely far
    from the median. A distance threshold was tried first and is not sufficient: on a near-even
    match (Khachanov 1.70 vs Atmane 2.15) an inverted row sits only 0.14 away in implied terms,
    inside any threshold loose enough to preserve genuine price disagreement. Asking the
    question directly has no such blind spot, and needs no tuning.

    Genuine keen prices are unaffected: a book pricing the favourite shorter still resembles
    the consensus home side far more than the away side.
    """
    priced = [
        (i, row["home_odds"], row["away_odds"])
        for i, row in enumerate(odds_rows)
        if row.get("home_odds") and row.get("away_odds")
    ]
    if len(priced) < 3:
        # Too few books to establish a consensus; trust them all rather than guess which is
        # wrong — showing the better of two prices beats discarding a real one.
        return set()

    consensus_home = _median([1.0 / home for _, home, _ in priced])
    consensus_away = _median([1.0 / away for _, _, away in priced])
    if abs(consensus_home - consensus_away) < 0.02:
        # A genuine coin-flip: "inverted" is not meaningfully different from "not inverted",
        # so there is nothing to detect and nothing worth dropping.
        return set()

    return {
        i
        for i, home, _ in priced
        if abs(1.0 / home - consensus_away) < abs(1.0 / home - consensus_home)
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
