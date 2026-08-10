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


# A decimal price above this is treated as not a real quote. Matches the threshold
# ml/training/train_football.py already applies (PLAUSIBLE_MAX_DECIMAL_ODDS), so training and
# serving agree on what counts as a usable price. 15.0 is comfortably above any realistic
# three-way football or two-way tennis price while excluding the 41.00-151.00 rows measured in
# real data against medians of 1.97-2.90.
PLAUSIBLE_MAX_DECIMAL_ODDS = 15.0


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


def latest_price_per_bookmaker(odds_rows: list[dict]) -> list[dict]:
    """Reduce an append-only price history to one CURRENT row per bookmaker.

    Odds rows are snapshots, appended and never updated by design, so a fixture accumulates
    8.7 rows per bookmaker on average and up to 47. Passing all of them into best_available_odds
    makes "best odds" a high-water mark over the whole history rather than a price anyone can
    still take.

    Measured over 119 real fixtures: 28% displayed a max that no bookmaker was still offering,
    overstating by 5.89% on average and 55% at worst. That inflates the number on the card, the
    expected value computed from it, and the min_odds filter that decides whether a fixture is
    shown at all -- a user could be sent to a price that had already gone.

    It also collapses names differing only by case. TheRundown writes "draftkings" and
    API-Football "Draftkings", so the same book counted twice toward the consensus median that
    decides which rows are inverted.

    Rows with no timestamp sort last rather than being dropped: an untimed price is still a
    real quote, and discarding it would silently shrink coverage.
    """
    latest: dict[tuple, dict] = {}
    for row in odds_rows:
        key = ((row.get("bookmaker") or "").strip().lower(), row.get("line"))
        seen = latest.get(key)
        if seen is None or (
            row.get("updated_at") is not None
            and (seen.get("updated_at") is None or row["updated_at"] > seen["updated_at"])
        ):
            latest[key] = row
    return list(latest.values())


def best_available_odds(odds_rows: list[dict]) -> dict[str, float | None]:
    """Best (highest) odds per side across all tracked bookmakers for one fixture. Used for
    both h2h (home/draw/away columns) and double_chance (which reuses the same home_odds/
    away_odds columns for its own two outcomes — see app/odds/models.py:Odds).

    Rows that contradict the consensus are excluded first — see _consensus_outliers — and
    implausible prices are excluded too, which is a genuinely different failure: a row can be
    the right way round and still absurd. Measured across real tennis h2h rows, HardRock
    supplied 151.00 where fourteen other books had a median of 1.97, plus 76.00 against 2.07
    and 61.00 against 2.90. None of those are inverted, so the consensus test passes them, and
    taking the MAXIMUM then actively selects them.

    ml/training/train_football.py already filters exactly this with PLAUSIBLE_MAX_DECIMAL_ODDS
    after an inflated ROI traced back to the same kind of row; serving simply never gained the
    same filter. Train/serve parity, not a new idea.

    Currently a latent gap rather than a live bug — MAX_EDGE_OVER_MARKET happens to reject the
    resulting pick, because a 151.00 price implies 0.7% and any real model probability exceeds
    that by far more than the guard allows. But relying on that is relying on a coincidence:
    the pick is dropped as untrustworthy rather than repriced, so a fixture with perfectly good
    odds elsewhere can lose its pick entirely because one absurd row won the selection."""
    outliers = _consensus_outliers(odds_rows)
    best = {"home": None, "draw": None, "away": None}
    for i, row in enumerate(odds_rows):
        if i in outliers:
            continue
        for side in ("home", "draw", "away"):
            value = row.get(f"{side}_odds")
            if value is None or value > PLAUSIBLE_MAX_DECIMAL_ODDS:
                continue
            if best[side] is None or value > best[side]:
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
