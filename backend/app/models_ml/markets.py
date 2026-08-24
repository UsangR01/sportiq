"""Derived football prediction markets beyond the core home/draw/away 1X2 (TDD §3.2/§4.2
extension) — double chance, Over/Under goals, Over/Under corners. Deliberately NOT new models:

- Double chance (1X, X2) is a pure arithmetic combination of the existing calibrated home/
  draw/away probabilities — no new data or training needed at all.
- Over/Under goals and Over/Under corners both come from treating each side's expected count
  (xg_home/xg_away from FootballModel's Layer 1; corners_xg_home/corners_xg_away from the new
  corners-Poisson-regressors, see app/models_ml/football.py) as independent Poisson-distributed
  random variables. The sum of two independent Poisson variables is itself Poisson with rate
  equal to the sum of the two rates — a standard, well-known result — so the total-goals (or
  total-corners) distribution is Poisson(xg_home + xg_away), and P(total > line) follows
  directly from that distribution's CDF. This reuses Layer 1's own output with no extra model
  or training pass, at the cost of the (documented, standard) independence assumption.

GOALS_LINES/CORNERS_LINES mirror the same constants in app/adapters/api_football.py (the only
lines this product actually surfaces/has real odds for) — kept as separate constants here
since this module must stay adapter-independent (also used by, e.g., historical retrodiction,
which has no live odds at all).
"""

import math

GOALS_LINES: tuple[float, ...] = (1.5, 2.5, 3.5, 4.5)
CORNERS_LINES: tuple[float, ...] = (9.5, 10.5)


def double_chance_probs(
    home_prob: float | None, draw_prob: float | None, away_prob: float | None
) -> tuple[float | None, float | None]:
    """Returns (P(1X) = P(home or draw), P(X2) = P(away or draw)). None if the inputs needed
    for that side are themselves missing (draw_prob is None for two-outcome sports, e.g. NBA —
    this module is football-only in practice, but stays honest about missing inputs rather
    than silently treating a missing draw_prob as 0)."""
    home_or_draw = (
        home_prob + draw_prob if home_prob is not None and draw_prob is not None else None
    )
    away_or_draw = (
        away_prob + draw_prob if away_prob is not None and draw_prob is not None else None
    )
    return home_or_draw, away_or_draw


def _poisson_pmf(k: int, rate: float) -> float:
    return math.exp(-rate) * rate**k / math.factorial(k)


def _poisson_cdf(k: int, rate: float) -> float:
    """P(X <= k) for X ~ Poisson(rate)."""
    return sum(_poisson_pmf(i, rate) for i in range(k + 1))


#: var/mean of real total corners, measured on 23,782 fixtures from the TRAIN seasons only
#: (2025 held out). Poisson assumes exactly 1.0.
#:
#: THIS IS THE OPPOSITE OF THE GOALS FINDING, AND BOTH ARE MEASURED. Total goals came out at
#: var/mean 1.003 across 8,718 fixtures, which is why a Negative Binomial was investigated for
#: goals and deliberately NOT built -- it would have fitted a dispersion that does not exist.
#: Nobody had measured CORNERS separately, and they are genuinely overdispersed.
#:
#: The cost of assuming Poisson was concentrated exactly where picks are drawn from: tails too
#: thin, so extreme probabilities came out too extreme. On the served model's test parquet,
#: P(over 9.5) at predicted >= 0.60 claimed 0.634 against an actual 0.566.
CORNERS_DISPERSION = 1.189


def _count_cdf(k: int, mean: float, dispersion: float) -> float:
    """P(total <= k) for a count with this mean, Poisson at dispersion 1.0 and Negative Binomial
    above it.

    Falls back to Poisson for dispersion <= 1: a Negative Binomial cannot represent var < mean at
    all, and this codebase has already measured 9 of 18 leagues as UNDERdispersed on goals -- so
    silently coercing one would be worse than the assumption it replaces.
    """
    if dispersion <= 1.0 or mean <= 0:
        return _poisson_cdf(k, mean)
    # Standard mean/variance parameterisation: var = dispersion * mean.
    prob = 1.0 / dispersion
    size = mean * prob / (1.0 - prob)
    from scipy.stats import nbinom

    return float(nbinom.cdf(k, size, prob))


def over_under_probs(
    expected_total: float | None,
    lines: tuple[float, ...],
    dispersion: float = 1.0,
) -> dict[float, tuple[float | None, float | None]]:
    """{line: (P(under line), P(over line))} for every line in `lines`, treating the total as
    Poisson(expected_total) — see module docstring. expected_total is xg_home + xg_away (goals)
    or corners_xg_home + corners_xg_away (corners); the caller is responsible for summing the
    two sides before calling this. Every line maps to (None, None) if expected_total itself is
    missing (never a fabricated 50/50 split), matching this codebase's "never fabricate a
    neutral value" convention."""
    if expected_total is None or expected_total < 0:
        return {line: (None, None) for line in lines}

    result: dict[float, tuple[float | None, float | None]] = {}
    for line in lines:
        # A .5 line has no exact-total edge case to worry about (total goals/corners are
        # always integers), so floor(line) is always the correct "under" boundary.
        under_prob = _count_cdf(math.floor(line), expected_total, dispersion)
        over_prob = 1.0 - under_prob
        result[line] = (under_prob, over_prob)
    return result
