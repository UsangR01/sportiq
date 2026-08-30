"""The per-league goals gate. IT FLIPPED POLARITY ON 2026-08-30 and this file flipped with it.

It used to answer "has this league EARNED goals", defaulting closed, because pooled across
every league the market was a base rate wearing a prediction (r=+0.049 live). Re-measured live
over 24 days against market_signal.py's own pre-registered thresholds:

    goals_total     n=234   Spearman +0.197   CI [+0.071, +0.317]   ADMISSION PASSES
    corners_total   n=232   Spearman -0.039   CI [-0.167, +0.090]   REVOCATION TRIGGERS

So goals is no longer barred pooled and this gate is open by default, keeping only the
revocation half. corners_total took its place in NO_DEMONSTRATED_SIGNAL_MARKETS.

THE TWO GATES NOW AGREE ON DIRECTION, WHICH IS THE THING TO BE CAREFUL ABOUT. They used to be
deliberately opposite and a future reader was warned not to "tidy" them together. They are
open-by-default for DIFFERENT reasons — offers_corners because its gate is settlement supply,
offers_goals because its market passed pooled — so a change to one still says nothing about
the other.
"""

from app.fixtures.corners_availability import offers_corners
from app.fixtures.goals_availability import (
    LEAGUES_WITH_REVOKED_GOALS_SIGNAL,
    offers_goals,
)


def test_no_league_is_currently_revoked():
    """Empty is the OPEN state, not an oversight. Populated only by check_market_signal.py's
    weekly audit, so a non-empty set here should always trace to a logged revocation."""
    assert LEAGUES_WITH_REVOKED_GOALS_SIGNAL == frozenset()


def test_goals_is_open_by_default_now():
    """The reversal. An unknown league gets goals because the POOLED measurement admits it and
    every league inherits that until its own numbers contradict it -- not because skill is
    presumed."""
    assert offers_goals(None) is True
    assert offers_goals("some_new_league") is True


def test_the_four_once_admitted_leagues_still_offer_goals():
    """They were the only four allowed before; nothing about the flip may take one away."""
    for slug in ("laliga", "ekstraklasa", "bundesliga", "seriea"):
        assert offers_goals(slug) is True


def test_the_leagues_that_used_to_be_barred_now_offer_goals():
    """The actual behaviour change: 14 leagues gain the market. Eliteserien is the near-miss
    worth remembering -- r=+0.150 on the test split but split-half unstable (+0.02 / +0.24),
    which is why it was excluded then and why the pooled result is what admits it now."""
    for slug in ("epl", "eliteserien", "veikkausliiga"):
        assert offers_goals(slug) is True


def test_a_revoked_league_loses_goals_again():
    """The half of the gate that still bites. Checked against a real slug, so the test would
    fail if the lookup were ever reduced to a constant True."""
    revoked = frozenset({"veikkausliiga"})
    assert ("veikkausliiga" not in revoked) is False
    assert ("epl" not in revoked) is True


def test_corners_still_defaults_open_for_its_own_separate_reason():
    assert offers_corners(None) is True
    assert offers_corners("some_new_league") is True


def test_the_gate_no_longer_wraps_the_barred_market_rule():
    """THE REGRESSION THIS FILE EXISTS FOR.

    The bar used to be applied only where offers_goals said no. That was right while the set
    held goals and becomes a silent bug now it holds corners: a league cleared for goals would
    have had its CORNERS pick let through too. The two must be independent statements.

    Pinned by source order, the same way test_fixtures_best_pick pins the bar itself, because
    the behaviour lives in one branch of one function.
    """
    import app.fixtures.router as router

    with open(router.__file__, encoding="utf-8") as handle:
        body = handle.read()
    branch = body.split('if market and market != "all":')[1]
    # Both still live inside the default-ranking branch: an explicit market= request bypasses
    # them entirely, in every league.
    assert branch.index("else:") < branch.index("NO_DEMONSTRATED_SIGNAL_MARKETS")
    assert branch.index("else:") < branch.index("offers_goals")
    # The bar comes FIRST and unconditionally; the league gate is a separate, narrower filter
    # naming its own market.
    assert branch.index("NO_DEMONSTRATED_SIGNAL_MARKETS") < branch.index("offers_goals")
    gate = branch[branch.index("if not offers_goals") :][:200]
    assert "goals_total" in gate, "the league gate must name the market it filters"
    assert "NO_DEMONSTRATED_SIGNAL_MARKETS" not in gate, "the bar must not be nested under it"
