"""grade_pick must agree with the phone, because disagreement is invisible and corrosive.

The rule lived only in mobile/lib/pickFormat.ts, so only the card could say whether a card had
been right; every measurement graded the 1X2 argmax instead. Hearts v Dundee Utd on 2026-08-09
is the case that exposed it -- the card showed UNDER 10.5 CORNERS at 2.05 and the match
finished 7-2 on corners (a WIN), while the history table recorded the fixture as wrong because
the 1X2 call was away against a 4-0 home win. Both statements were true about their own object.

These tests pin the PORT, including the parts that look like bugs and are parity.
"""

import pytest

from app.predictions.grading import actual_outcome, grade_pick


@pytest.mark.parametrize(
    "home, away, expected",
    [(2, 1, "home"), (0, 3, "away"), (1, 1, "draw"), (0, 0, "draw")],
)
def test_actual_outcome(home, away, expected):
    assert actual_outcome(home, away) == expected


def test_the_reported_fixture_grades_as_a_win():
    """Hearts v Dundee Utd: 4-0 on goals, 7-2 on corners, card showed under 10.5 corners.
    Nine corners is under the line, so the CARD was right -- even though the same fixture's
    1X2 call was wrong, which is the only thing the history table had ever measured."""
    assert grade_pick("corners_total", "under", 10.5, 4, 0, home_corners=7, away_corners=2) is True
    assert grade_pick("h2h", "away", None, 4, 0) is False


@pytest.mark.parametrize(
    "selection, home, away, expected",
    [
        ("home", 2, 1, True),
        ("home", 1, 2, False),
        ("draw", 1, 1, True),
        ("away", 0, 1, True),
    ],
)
def test_h2h(selection, home, away, expected):
    assert grade_pick("h2h", selection, None, home, away) is expected


@pytest.mark.parametrize(
    "selection, home, away, expected",
    [
        ("1X", 2, 0, True),
        ("1X", 1, 1, True),
        ("1X", 0, 2, False),
        ("X2", 0, 2, True),
        ("X2", 1, 1, True),
        ("X2", 2, 0, False),
    ],
)
def test_double_chance(selection, home, away, expected):
    assert grade_pick("double_chance", selection, None, home, away) is expected


def test_a_12_double_chance_is_ungraded_because_the_phone_does_not_grade_it_either():
    """PARITY, NOT AN OVERSIGHT. mobile/lib/pickFormat.ts handles only 1X and X2 and returns
    null for anything else. Grading "12" here would make the measurement disagree with the card
    it is supposed to be measuring -- the exact defect this module was written to close. Fix it
    in both places or neither."""
    assert grade_pick("double_chance", "12", None, 2, 0) is None


@pytest.mark.parametrize(
    "selection, line, home, away, expected",
    [
        ("over", 2.5, 2, 1, True),
        ("under", 2.5, 1, 1, True),
        ("under", 2.5, 2, 1, False),
        ("over", 3.5, 2, 1, False),
    ],
)
def test_goals_total(selection, line, home, away, expected):
    assert grade_pick("goals_total", selection, line, home, away) is expected


def test_an_exact_push_grades_false_on_both_sides():
    """Parity again: the phone uses strict inequalities both ways, so a total landing exactly
    on the line is a loss for over AND under rather than a void. Unreachable today -- every
    line in GOALS_LINES/CORNERS_LINES is a .5 line -- and pinned so an integer line cannot be
    introduced without someone seeing this."""
    assert grade_pick("goals_total", "over", 3.0, 2, 1) is False
    assert grade_pick("goals_total", "under", 3.0, 2, 1) is False


def test_corners_without_real_counts_is_unverifiable_not_wrong():
    """None means we cannot tell. Corners are null for NBA and for anything settled before
    fixture_live_state gained its corner columns; scoring those as losses would invent a losing
    record out of missing data."""
    assert grade_pick("corners_total", "under", 9.5, 1, 0) is None
    assert grade_pick("corners_total", "under", 9.5, 1, 0, home_corners=4) is None
    assert grade_pick("corners_total", "under", 9.5, 1, 0, home_corners=4, away_corners=3) is True


@pytest.mark.parametrize("result_type", ["retired", "walkover"])
def test_a_voided_match_is_never_graded(result_type):
    """A retirement is shown as VOID rather than a win or a loss, because most bookmakers void
    the bet -- a green tick would imply a payout the user may never have received. The phone
    applies this in FixtureCard.tsx before it ever calls the grader."""
    assert grade_pick("h2h", "home", None, 2, 0, result_type=result_type) is None


def test_an_unplayed_or_unknown_market_is_ungraded():
    assert grade_pick("h2h", "home", None, None, None) is None
    assert grade_pick("some_future_market", "yes", None, 2, 0) is None


def test_a_missing_line_cannot_be_graded():
    assert grade_pick("goals_total", "over", None, 2, 1) is None
    assert grade_pick("corners_total", "over", None, 2, 1, home_corners=6, away_corners=5) is None
