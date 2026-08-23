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

  ADDING ONE AGAIN, 2026-08-23. The second fix pinned settled fixtures to the OLD floor as a
  fixed constant, which surfaced any sub-0.35 pick once it settled REGARDLESS of when it was
  predicted. Reported as "we didn't have Hull vs Manchester United on the card but surprisingly
  it came on after the game was completed" -- completeness 0.265, predicted nine hours before
  kickoff and correctly hidden the whole time the match was playable. Eleven cards were doing it
  and not one predated the raise.

Both are the same defect: the past changing under the user. So a settled fixture is judged
against the floor that applied WHEN THAT PREDICTION WAS MADE -- which is a date comparison, not
a constant.
"""

from datetime import timedelta

from app.fixtures.router import (
    FEATURE_COMPLETENESS_FLOOR_RAISED_AT,
    MIN_FEATURE_COMPLETENESS,
    SETTLED_FEATURE_COMPLETENESS_FLOOR,
    _MarketCandidate,
    _pick_best,
)

OLD_ERA = FEATURE_COMPLETENESS_FLOOR_RAISED_AT - timedelta(days=4)
NEW_ERA = FEATURE_COMPLETENESS_FLOOR_RAISED_AT + timedelta(days=9)


def candidate(
    probability=0.72,
    completeness=None,
    market="corners_total",
    selection="under",
    as_of=OLD_ERA,
):
    return _MarketCandidate(selection, probability, 2.05, market, 10.5, completeness, as_of=as_of)


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


def test_the_same_completeness_flips_on_when_the_prediction_was_made():
    """THE THIRD REPORT, and the reason a constant was not enough.

    0.30 clears the old floor and fails today's. Whether a settled card should show it depends
    entirely on which floor was in force when the prediction was written — not on the fact that
    the match has since finished.
    """
    assert _pick_best([candidate(completeness=0.30, as_of=OLD_ERA)], is_settled=True) is not None
    assert _pick_best([candidate(completeness=0.30, as_of=NEW_ERA)], is_settled=True) is None


def test_the_raise_date_matches_the_floors_it_arbitrates():
    """A sanity check on the constants themselves: the settled floor must be the LOWER of the
    two, or the date comparison would be hiding picks rather than preserving them."""
    assert SETTLED_FEATURE_COMPLETENESS_FLOOR < MIN_FEATURE_COMPLETENESS
