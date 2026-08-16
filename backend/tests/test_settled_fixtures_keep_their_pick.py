"""A decided fixture is a RECORD, not a recommendation, so bet-worthiness guards do not apply.

THE REPORTED BUG. A user recognised a Hearts v Dundee Utd card from 2026-08-09 that had shown
UNDER 10.5 CORNERS at 2.05. The match finished 7-2 on corners, so the pick WON. Days later the
card had no pick at all. Nothing about the fixture changed -- MIN_FEATURE_COMPLETENESS was
raised 0.25 -> 0.35 on 2026-08-13, that prediction sits at 0.32, and best_pick is recomputed on
every request. A guard introduced after the fact reached backwards and deleted a result the
product had already published.

WHY THIS IS A CORRECTNESS BUG AND NOT A PRESENTATION ONE. Retroactive filtering is not neutral:
sub-0.35 picks measure 0.2857 accuracy against 0.5263 at or above it, so silently dropping them
from history makes the visible track record BETTER than the product's real one. A history that
improves every time a guard tightens is not a history. 21 picks across 14 days had been erased
this way, 9 of them Scottish Premiership -- which is exactly what the report described.

The measurement that raised the floor said "a measured cost of ZERO upcoming picks". That was
true, and it was the wrong population: the floor lives in _pick_best, which runs for every
fixture the feed renders, including settled ones being reviewed for how the model did.

NOT COVERED HERE, because it is unrecoverable: this does not replay the card as SHOWN. Ranking
still uses today's odds. pick_snapshots is the only thing that can do that and holds nothing
before 2026-08-10 -- the reported fixture is 08-09.
"""

import pytest

from app.fixtures.router import (
    MAX_EDGE_OVER_MARKET,
    MIN_FEATURE_COMPLETENESS,
    _MarketCandidate,
    _pick_best,
)


def _candidate(
    probability,
    *,
    completeness=None,
    odds=None,
    selection="under",
    market="corners_total",
    line=10.5,
):
    return _MarketCandidate(
        selection=selection,
        probability=probability,
        odds=odds,
        market=market,
        line=line,
        feature_completeness=completeness,
    )


def test_the_reported_fixture_keeps_its_pick():
    """Hearts v Dundee Utd, verified against the real row: under 10.5 corners at 2.05 on a
    prediction with feature_completeness 0.32, below today's 0.35 floor."""
    candidate = _candidate(0.7105, completeness=0.32, odds=2.05)
    assert _pick_best([candidate], is_settled=True) is not None
    assert _pick_best([candidate]) is None, "the guard must still bite before kickoff"


def test_the_completeness_floor_still_protects_upcoming_fixtures():
    """The floor exists for a real reason -- Tottenham v Newcastle served 1X at 99.7% off 3 of
    31 features.

    CORRECTED 2026-08-16. This used to assert that a 0.10 vector DID surface once settled, which
    was the first fix overshooting: it exempted settled fixtures from the floor entirely, so it
    also revealed picks that never cleared the OLD floor and were therefore shown to nobody
    while they were live. Production carried five of those, worst being away at 1.00 on a 0.23
    vector. A settled fixture is now judged against the floor that applied when it WAS live --
    see SETTLED_FEATURE_COMPLETENESS_FLOOR."""
    empty_vector = _candidate(0.997, completeness=0.10, market="h2h", selection="home", line=None)
    assert _pick_best([empty_vector]) is None
    assert _pick_best([empty_vector], is_settled=True) is None


def test_the_other_guards_are_deliberately_NOT_exempted():
    """THE SCOPE OF THE FIX, and it was narrowed after trying it wider.

    Exempting every bet-worthiness guard sounds tidier and rewrites history the other way:
    lifting MIN_EDGE_OVER_BASE_RATE surfaced a 1X pick sitting BELOW its own base rate on a
    fixture that had been correctly hidden all along, and lifting the barred-market rule put
    goals under-4.5 at 1.18 on the reported card in place of the corners pick actually shown.
    Adding a pick that was never shown is the same defect as deleting one that was.

    So only the guard that TIGHTENED and erased is exempted. These two must keep biting."""
    reckless = _candidate(0.95, odds=4.00)
    assert 0.95 - (1 / 4.00) > MAX_EDGE_OVER_MARKET
    assert _pick_best([reckless], is_settled=True) is None

    uninformative = _candidate(0.50, market="h2h", selection="home", line=None, odds=2.00)
    assert _pick_best([uninformative], sport_slug="football", is_settled=True) is None


def test_the_users_own_slider_still_applies_to_settled_fixtures():
    """min_probability is NOT a bet-worthiness guard -- it is a control the user can see and
    move, and a card vanishing when they raise their own slider is the control working. The
    guards this exemption disables are the invisible ones."""
    # The real reported pick (0.7105), so the slider is the only thing under test -- 0.55 was
    # tried first and failed for an unrelated reason: it sits inside corners' own base rate and
    # the informativeness gate refused it, which still applies to settled fixtures.
    candidate = _candidate(0.7105, completeness=0.32, odds=2.05)
    assert _pick_best([candidate], min_probability=0.75, is_settled=True) is None
    assert _pick_best([candidate], min_probability=0.60, is_settled=True) is not None


@pytest.mark.parametrize("completeness", [MIN_FEATURE_COMPLETENESS - 0.01, 0.30, 0.25])
def test_a_settled_pick_that_cleared_the_old_floor_still_shows(completeness):
    """Everything the pre-raise product actually published stays published.

    RENAMED from test_any_completeness_passes_once_the_result_is_known, and 0.0 dropped from the
    parameters: "any completeness" was the overshoot. Zero features is not a pick that was ever
    shown, so restoring it is inventing history rather than preserving it."""
    assert _pick_best([_candidate(0.71, completeness=completeness, odds=2.05)], is_settled=True)


@pytest.mark.parametrize("completeness", [0.24, 0.10, 0.0])
def test_a_settled_pick_below_the_old_floor_stays_hidden(completeness):
    """It was suppressed for its entire live life, so no user ever saw it."""
    assert (
        _pick_best([_candidate(0.71, completeness=completeness, odds=2.05)], is_settled=True)
        is None
    )
