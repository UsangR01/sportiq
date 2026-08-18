"""form_pts is a ROLLING window, and serving was computing a season average.

Reported: every MLS card showed 1X at over 90%. Measured on production, MLS away-win
probability averaged 0.048 across 30 scheduled fixtures, against 0.21-0.25 in the leagues that
looked normal, and all 30 picks were the same market.

CAUSE. _parse_form_points averaged API-Football's ENTIRE form string -- the season to date --
while training computes the same feature over the last LAST_N_FORM matches
(football_features._rolling_form). A train/serve mismatch on a core input that grows with every
match played: invisible in a league that has just kicked off, worst in a long season.

The real case, Philadelphia Union 19 matches in, form LLLLLLWDDLDLLDLWWWW:

    whole string       5W 4D 10L = 19 points / 19 = 1.0   <- what the model was told
    last 10 (LDLLDLWWWW)              14 points / 10 = 1.4   <- what training means
    last 5  (LWWWW)                   12 points /  5 = 2.4   <- for scale only

Their actual last four fixtures were four wins, confirmed against the provider. So win_streak
said 4.0 while form_pts said the team was the worst in the league. Across a mid-season league
this compresses everyone toward the same mid-table number, the feature stops separating
anybody, and the model falls back on home advantage.

The correction is 1.0 -> 1.4, not the 1.0 -> 2.4 first claimed: 2.4 is a FIVE-match window and
LAST_N_FORM is 10. Recorded because the wrong figure was stated before this test was run, and
the test is what caught it.
"""

from app.adapters.api_football import _parse_form_points, _parse_streaks
from app.models_ml.football_features import LAST_N_FORM

# The real string read live from API-Football on 2026-08-18.
PHILADELPHIA = "LLLLLLWDDLDLLDLWWWW"


def test_the_reported_case_reads_recent_form_not_the_season():
    """1.0 was the season average and is what produced the degenerate MLS cards."""
    assert _parse_form_points(PHILADELPHIA) == 1.4
    season_average = sum({"W": 3, "D": 1, "L": 0}[c] for c in PHILADELPHIA) / len(PHILADELPHIA)
    assert season_average == 1.0


def test_form_points_and_the_streak_no_longer_contradict():
    """Both read the same string. A side on a four-win streak cannot also be the league's worst
    on form -- that contradiction is what exposed this. The gap narrows rather than closing
    entirely, which is correct: a ten-match window still carries six of their bad results."""
    win_streak, _losing = _parse_streaks(PHILADELPHIA)

    assert win_streak == 4.0
    assert _parse_form_points(PHILADELPHIA) > 1.0


def test_the_window_matches_the_training_constant():
    """PARITY. Training computes this over LAST_N_FORM matches; serving must use the same
    number or the model is fed a differently-shaped feature than it learned on."""
    long_season = "W" * 40 + "L" * LAST_N_FORM

    assert _parse_form_points(long_season) == 0.0, "only the tail may count"

    long_season_2 = "L" * 40 + "W" * LAST_N_FORM
    assert _parse_form_points(long_season_2) == 3.0


def test_a_short_season_uses_everything_it_has():
    """Early in a season there are fewer than LAST_N_FORM matches, and that is not an error --
    it is simply all the form that exists."""
    assert _parse_form_points("WWD") == (3 + 3 + 1) / 3


def test_the_most_recent_match_is_last():
    """The direction is load-bearing and was verified against real fixtures rather than
    assumed: Philadelphia's string ENDS WWWW and their last four results were four wins. Read
    from the wrong end, this feature would report the start of the season forever."""
    assert _parse_form_points("LLLLLLLLLLWWWWWWWWWW") == 3.0
    assert _parse_form_points("WWWWWWWWWWLLLLLLLLLL") == 0.0


def test_no_form_is_still_none():
    """Never a fabricated neutral value — a team with no matches played has no form."""
    assert _parse_form_points(None) is None
    assert _parse_form_points("") is None
    assert _parse_form_points("???") is None
