"""Does a market's prediction actually track reality? Measured, on a pre-registered trigger.

WHY THIS EXISTS AS CODE RATHER THAN A NOTE

`app/fixtures/router.py:NO_DEMONSTRATED_SIGNAL_MARKETS` bars `goals_total` from winning the
headline pick. That bar was imposed on ONE measurement — the correlation between the predicted
total (xg_home + xg_away) and the actual total goals, on real settled fixtures:

    goals_total     n=242   r=+0.049   0.2% of variance   -> barred
    corners_total   n=234   r=+0.288   8.3% of variance   -> kept

Layer 1 was later tuned and the market's test-set discrimination improved markedly (under-3.5
trend z +5.97 -> +8.79; under-4.5 went from an INVERTED bucket order to monotonic). That is
encouraging and it is NOT the same evidence. The test split is a completed season with full
feature vectors; the live feed runs on much thinner ones (EPL 0.12, Scottish Prem 0.19
completeness measured). Lifting a bar set on live evidence using test evidence would be
swapping the yardstick mid-measurement.

So the bar stays until the live number is refreshed, and the condition for refreshing it is
fixed HERE, in advance, so it cannot be argued after the number is known.

THE THRESHOLDS ARE ANCHORED, NOT PICKED

  MIN_N = 200          enough that r's 95% interval is roughly +/-0.14 wide, so +0.15 is
                       separable from +0.05 at all.
  MIN_R = 0.15         sits between the +0.049 that failed and the +0.288 that corners passed
                       on: "clearly better than what we rejected", without demanding it match
                       the market we kept.
  MIN_CI_LOW = 0.05    the interval must exclude the level that got it barred. This is the one
                       that matters — a point estimate of +0.15 on a wide interval is not
                       evidence of improvement, it is evidence of a small sample.

Only predictions from MIN_MODEL_VERSION onward count. Every one of the original 242 was made
by the pre-tuning model, so including them would measure the model being replaced.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The first model version whose predictions are eligible — the Layer 1 tuning run. Versions sort
# lexicographically because they are `football_xgb_vYYYYMMDDHHMMSS`.
MIN_MODEL_VERSION = "football_xgb_v20260812080921"

MIN_N = 200
MIN_R = 0.15
MIN_CI_LOW = 0.05


@dataclass(frozen=True)
class MarketSignal:
    """Correlation between what a market predicted and what happened."""

    market: str
    n: int
    r: float | None
    ci_low: float | None
    ci_high: float | None

    @property
    def variance_explained(self) -> float | None:
        return None if self.r is None else self.r**2

    @property
    def meets_trigger(self) -> bool:
        """All three conditions, deliberately. n alone permits a confident wrong number; r alone
        permits a lucky small sample; the interval is what ties the claim to the evidence."""
        if self.r is None or self.ci_low is None:
            return False
        return self.n >= MIN_N and self.r >= MIN_R and self.ci_low > MIN_CI_LOW

    def describe(self) -> str:
        if self.r is None:
            return f"{self.market}: n={self.n} — too few settled predictions to correlate yet"
        verdict = "TRIGGER MET" if self.meets_trigger else "below trigger"
        return (
            f"{self.market}: n={self.n} r={self.r:+.3f} "
            f"95% CI [{self.ci_low:+.3f}, {self.ci_high:+.3f}] "
            f"({self.variance_explained:.1%} of variance) — {verdict}"
        )


def pearson_with_ci(
    xs: list[float], ys: list[float]
) -> tuple[float | None, float | None, float | None]:
    """Pearson r plus a Fisher-z 95% interval.

    The interval is the point of this function. A bare r invites reading +0.15 on n=30 as an
    improvement over +0.049 on n=242, which it is not.
    """
    n = len(xs)
    if n != len(ys):  # strict=True below would raise; refusing early is the honest answer
        return None, None, None
    if n < 4:
        return None, None, None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None, None, None
    r = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / math.sqrt(sxx * syy)
    r = max(min(r, 0.999999), -0.999999)
    z = math.atanh(r)
    se = 1.0 / math.sqrt(n - 3)
    return r, math.tanh(z - 1.96 * se), math.tanh(z + 1.96 * se)


async def measure_goals_total_signal(db: AsyncSession) -> MarketSignal:
    """Predicted total goals vs actual, on settled fixtures from the eligible model onward.

    Restricted to `pre_match` predictions: a retrodiction is produced after the result is known
    and would flatter the correlation, which is the whole reason `predictions.kind` exists.
    """
    rows = (
        await db.execute(
            text("""
                select (p.xg_home + p.xg_away) as predicted,
                       (o.home_score + o.away_score) as actual
                from predictions p
                join fixtures f on f.id = p.fixture_id
                join sports s on s.id = f.sport_id
                join outcomes o on o.fixture_id = f.id
                where s.slug = 'football'
                  and p.kind = 'PRE_MATCH'
                  and p.xg_home is not null
                  and p.xg_away is not null
                  and p.model_version >= :min_version
                """),
            {"min_version": MIN_MODEL_VERSION},
        )
    ).all()
    xs = [float(row[0]) for row in rows]
    ys = [float(row[1]) for row in rows]
    r, lo, hi = pearson_with_ci(xs, ys)
    return MarketSignal(market="goals_total", n=len(rows), r=r, ci_low=lo, ci_high=hi)
