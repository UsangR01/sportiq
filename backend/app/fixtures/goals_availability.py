"""Which leagues have DEMONSTRATED goals-market signal — the opposite polarity to corners.

corners_availability.py answers "can this league SETTLE a corners pick promptly" and defaults to
YES, because that gate is about data supply and an unmeasured league deserves the benefit of the
doubt. This gate is about MODEL SKILL, and the default runs the other way: goals_total stays
barred (NO_DEMONSTRATED_SIGNAL_MARKETS in router.py) unless a league has EARNED inclusion with a
measurement. Pooled across all 18 leagues the market is a base rate wearing a prediction —
r=+0.049 live when barred — and a market must demonstrate signal before users are told to bet
on it, never the reverse.

MEASURED PER LEAGUE, 2026-08-18, on the held-out 2025 test season of the served model
(ml/evaluation/test_predictions_football_xgb_v20260813153115.parquet), against the same
pre-registered thresholds the live re-admission trigger uses (market_signal.py: r >= 0.15 with
the 95% CI's lower bound above 0.05):

    laliga         n=380  r=+0.201  CI [+0.102, +0.296]   split-half +0.246 / +0.149
    ekstraklasa    n=306  r=+0.180  CI [+0.069, +0.286]   split-half +0.241 / +0.085  <- weakest
    bundesliga     n=308  r=+0.175  CI [+0.064, +0.281]   split-half +0.156 / +0.203
    seriea         n=380  r=+0.153  CI [+0.053, +0.250]   split-half +0.131 / +0.160
    ---- bar ----
    eliteserien    n=241  r=+0.150  CI [+0.024, ...]      unstable (+0.023 / +0.243), excluded
    every other league    r <= +0.14, most under +0.10

The four admitted leagues' under-3.5 reliability buckets are monotonic and within ~0.01 of the
diagonal (pooled n=1,374), so the probabilities shown are honest as well as discriminating.

STATED PLAINLY: this is TEST-SPLIT evidence, and market_signal.py's pre-registration says the
pooled bar lifts on LIVE evidence. The per-league gate is a deliberate, user-directed exception
to that, taken because live evidence for these leagues is structurally unavailable — their
seasons opened the same week — and withholding the market all season pending data that cannot
exist yet serves nobody. The compensating control is real: check_market_signal.py now measures
the LIVE per-league signal weekly, and a gated league whose live interval settles BELOW the bar
(n >= MIN_N and ci_high < MIN_R) is flagged for revocation. The gate is auditable, not a leap
of faith.

Re-derive candidates from a training run's per-fixture parquet rather than editing by hand —
the measurement block above is reproducible from that file alone.
"""

# Leagues whose goals_total predictions cleared the pre-registered signal bar per league.
# Empty this set to bar goals from the headline pick everywhere again.
LEAGUES_WITH_DEMONSTRATED_GOALS_SIGNAL = frozenset(
    {
        "laliga",
        "ekstraklasa",
        "bundesliga",
        "seriea",
    }
)


def offers_goals(league_slug: str | None) -> bool:
    """True only for a league measured to have real goals signal.

    An unknown league returns False — the OPPOSITE default to offers_corners, deliberately:
    corners defaults open because its gate is about settlement supply, while this gate is about
    demonstrated model skill, and skill is earned by measurement, never presumed. A league with
    no measurement gets no goals headline pick."""
    if not league_slug:
        return False
    return league_slug in LEAGUES_WITH_DEMONSTRATED_GOALS_SIGNAL
