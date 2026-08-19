"""The per-league goals gate: earned inclusion, opposite polarity to corners.

goals_total is barred from the headline pick (NO_DEMONSTRATED_SIGNAL_MARKETS) because pooled
across every league it is a base rate wearing a prediction (r=+0.049 live). Four leagues
measured real signal on the held-out test season — see goals_availability.py for the numbers —
and only those four get the market back.

The polarity difference from corners is the thing a future reader would 'tidy up' and must not:
offers_corners defaults OPEN (its gate is settlement supply — an unmeasured league gets the
benefit of the doubt), offers_goals defaults CLOSED (its gate is model skill — earned by
measurement, never presumed).
"""

from app.fixtures.corners_availability import offers_corners
from app.fixtures.goals_availability import (
    LEAGUES_WITH_DEMONSTRATED_GOALS_SIGNAL,
    offers_goals,
)


def test_admitted_leagues_are_exactly_the_measured_ones():
    """Pinned so a league cannot drift in OR OUT without a measurement behind it. Re-derived
    2026-08-19 on the served model's parquet: ekstraklasa (+0.145) and seriea (+0.116) fell
    below the bar they had passed three retrains earlier, ucl (+0.200) earned admission.
    Eliteserien remains the near-miss worth remembering: excluded for split-half instability."""
    assert LEAGUES_WITH_DEMONSTRATED_GOALS_SIGNAL == {
        "laliga",
        "bundesliga",
        "ucl",
    }


def test_goals_defaults_closed_where_corners_defaults_open():
    assert offers_goals(None) is False
    assert offers_corners(None) is True
    assert offers_goals("some_new_league") is False
    assert offers_corners("some_new_league") is True


def test_admitted_league_offers_goals():
    assert offers_goals("laliga") is True
    assert offers_goals("bundesliga") is True


def test_unadmitted_leagues_stay_barred():
    # The pooled-live measurement that imposed the bar; the near-miss; a signal-free league;
    # and the two 2026-08-19 removals, so a revert cannot slip back in silently.
    assert offers_goals("epl") is False
    assert offers_goals("eliteserien") is False
    assert offers_goals("veikkausliiga") is False
    assert offers_goals("ekstraklasa") is False
    assert offers_goals("seriea") is False
    # Cups: only ucl measured signal; the other two fail the same bar.
    assert offers_goals("ucl") is True
    assert offers_goals("uel") is False
    assert offers_goals("uecl") is False


def test_the_gate_sits_inside_the_default_ranking_branch_only():
    """An explicit market=goals_total request must still be honoured in every league — the gate
    decides what wins the DEFAULT cross-market ranking, mirroring the barred-market rule it
    scopes. Pinned the same way test_fixtures_best_pick pins the bar itself: by source order,
    since the behaviour lives in one branch of one function."""
    import app.fixtures.router as router

    with open(router.__file__, encoding="utf-8") as handle:
        body = handle.read()
    branch = body.split('if market and market != "all":')[1]
    assert branch.index("else:") < branch.index("offers_goals")
    # The bar still applies when the gate says no — offers_goals guards it, not replaces it.
    assert branch.index("offers_goals") < branch.index("NO_DEMONSTRATED_SIGNAL_MARKETS")
