"""A settled fixture keeps the pick it was SHOWN -- and only that one.

Two opposite failures, both real, both reported:

  REMOVING a pick that was shown. Raising MIN_FEATURE_COMPLETENESS 0.25 -> 0.35 on 2026-08-13
  reached backwards and deleted the Hearts v Dundee Utd card from 08-09 -- under-10.5 corners at
  2.05, on a 0.32 vector, a pick that had been published and had WON. best_pick is recomputed
  per request, so a guard tightened after the fact rewrote history. 21 picks over 14 days went.

  ADDING a pick that was never shown. The first fix exempted settled fixtures from the floor
  ENTIRELY, which also surfaced picks that never cleared the OLD floor either -- suppressed the
  whole time they were live, shown to nobody. Measured in production 2026-08-16, five of them,
  worst being Chongqing Tongliang Long v SHANGHAI SIPG at away 1.00 on a 0.23 vector.

Both are the same defect: the past changing under the user. So a settled fixture is judged
against the floor that applied WHEN IT WAS LIVE.
"""

from app.fixtures.router import (
    MIN_FEATURE_COMPLETENESS,
    SETTLED_FEATURE_COMPLETENESS_FLOOR,
    _MarketCandidate,
    _pick_best,
)


def candidate(probability=0.72, completeness=None, market="corners_total", selection="under"):
    return _MarketCandidate(selection, probability, 2.05, market, 10.5, completeness)


def test_the_hearts_card_survives_the_floor_being_raised():
    """THE ORIGINAL REPORT. 0.32 sits below today's 0.35 but above the floor that applied when
    the card was published, so a settled fixture must keep it."""
    pick = _pick_best([candidate(completeness=0.32)], sport_slug="football", is_settled=True)

    assert pick is not None
    assert pick.market == "corners_total"


def test_a_pick_that_never_cleared_the_old_floor_stays_hidden():
    """THE OVERSHOOT. 0.23 was below the floor the entire time the fixture was live, so it was
    never shown -- and a settled card must not invent it."""
    assert (
        _pick_best([candidate(completeness=0.23)], sport_slug="football", is_settled=True) is None
    )


def test_an_upcoming_fixture_is_still_judged_against_the_current_floor():
    """The exemption is about the past. A live card gets today's standard."""
    assert _pick_best([candidate(completeness=0.32)], sport_slug="football") is None
    assert _pick_best([candidate(completeness=0.45)], sport_slug="football") is not None


def test_an_unmeasured_vector_still_passes():
    """feature_completeness was added without a backfill, so NULL means "never measured", not
    "measured as bad". Treating it as failing would erase every prediction older than that
    migration."""
    assert _pick_best([candidate(completeness=None)], sport_slug="football", is_settled=True)
    assert _pick_best([candidate(completeness=None)], sport_slug="football")


def test_the_settled_floor_is_looser_than_the_live_one_but_not_absent():
    """Pins the relationship rather than the numbers: a settled fixture is more permissive than
    a live one, and neither is unbounded."""
    assert 0 < SETTLED_FEATURE_COMPLETENESS_FLOOR < MIN_FEATURE_COMPLETENESS
