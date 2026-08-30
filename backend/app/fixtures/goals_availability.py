"""Whether a league may lead with the goals market — now open by default, with revocation.

THE POLARITY FLIPPED ON 2026-08-30, and the reason is the whole point of pre-registering.

This file used to answer "has this league EARNED goals", defaulting closed, because pooled
across all 18 leagues the market measured r=+0.049 live and a market must demonstrate signal
before users are told to bet on it. Re-measured on live settled fixtures over 24 days, using
market_signal.py's own thresholds fixed long before this number existed:

    goals_total     n=234   Spearman +0.197   CI [+0.071, +0.317]   -> ADMISSION PASSES
    corners_total   n=232   Spearman -0.039   CI [-0.167, +0.090]   -> REVOCATION TRIGGERS

Goals clears MIN_N, MIN_R and MIN_CI_LOW together, so the pooled bar that made this gate
necessary is gone (see router.NO_DEMONSTRATED_SIGNAL_MARKETS, which now holds corners instead).
A per-league inclusion list on top of a market that passes pooled would keep 14 leagues out on
evidence that no longer says to.

WHAT SURVIVES IS THE REVOCATION HALF, and it is not decoration: check_market_signal.py measures
this live every week, and a league whose own interval settles below the bar goes in the set
below. Open by default is a measurement, not an assumption, and it is auditable either way.

THE ORIGINAL PER-LEAGUE ADMISSION EVIDENCE IS KEPT BELOW rather than deleted -- it is what
justified the four-league exception while the pooled bar stood, and a future re-bar should be
able to read it.

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

Re-derive candidates from a training run's per-fixture parquet rather than editing by hand --
the measurement block above is reproducible from that file alone.
"""

# Leagues whose LIVE goals signal has settled BELOW the bar they are served on.
#
# EMPTY IS THE OPEN STATE, not an oversight -- goals passes pooled, so every league leads with
# it unless its own live evidence says otherwise. Populated from check_market_signal.py's
# weekly audit, never by hand: the condition is n >= MIN_N with the 95% CI's upper bound below
# MIN_R, i.e. the interval excludes the level that would admit it.
#
# Adding a league here is the same act as barring a market pooled, one league wide.
LEAGUES_WITH_REVOKED_GOALS_SIGNAL: frozenset[str] = frozenset()


def offers_goals(league_slug: str | None) -> bool:
    """True unless this league's own live evidence has put it below the bar.

    OPEN BY DEFAULT since 2026-08-30, when goals cleared the pooled trigger -- the opposite of
    what this function did before, and the reason an unknown league now returns True. That
    still is not "presumed skill": the presumption is the POOLED MEASUREMENT, which every
    league inherits until its own numbers contradict it.
    """
    if league_slug is None:
        return True
    return league_slug not in LEAGUES_WITH_REVOKED_GOALS_SIGNAL
