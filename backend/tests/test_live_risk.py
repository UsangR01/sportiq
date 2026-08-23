"""The live at-risk rules (design spec §4.1).

Pure functions over a scoreline, so every case here is exact. The tests that matter most are the
ones asserting UNKNOWN: a wrong tag on a live card is a claim about a match in progress, and the
markets where we have no in-play data are precisely the ones where a plausible guess would be
indistinguishable from knowledge.
"""

from __future__ import annotations

import pytest

from app.predictions.live_risk import PickState, evaluate, should_alert


def _state(**kwargs) -> PickState:
    base = dict(
        sport_slug="football",
        market="h2h",
        selection="home",
        line=None,
        home_score=0,
        away_score=0,
        match_minute=30,
    )
    return evaluate(**{**base, **kwargs})


# --- what cannot be judged ---------------------------------------------------------------


def test_corners_never_gets_a_state_because_no_in_play_data_exists() -> None:
    """home_corners is written ONCE, at settlement. There is no in-play corner count to read, so
    any tag would be invented. This is a data fact, not a product preference."""
    assert (
        _state(market="corners_total", selection="over", line=9.5, home_score=1, away_score=1)
        is PickState.UNKNOWN
    )


def test_basketball_gets_no_state_because_it_reports_no_clock() -> None:
    """minute and period are populated on 0 of 263 rows. A six-point deficit in the first quarter
    and the same with a minute left are indistinguishable to us."""
    assert (
        _state(sport_slug="nba", home_score=80, away_score=95, match_minute=None)
        is PickState.UNKNOWN
    )


def test_a_fixture_with_no_score_yet_is_unknown_not_on_track() -> None:
    """Absent data must never read as good news."""
    assert _state(home_score=None, away_score=None) is PickState.UNKNOWN


def test_football_without_a_minute_is_unknown() -> None:
    """Every football rule keys off the clock; without it, being behind says nothing."""
    assert _state(home_score=0, away_score=2, match_minute=None) is PickState.UNKNOWN


# --- football h2h ------------------------------------------------------------------------


def test_a_leading_or_level_pick_is_on_track() -> None:
    assert _state(home_score=1, away_score=0) is PickState.ON_TRACK
    assert _state(home_score=1, away_score=1) is PickState.ON_TRACK


def test_one_goal_down_early_is_not_yet_at_risk() -> None:
    """Ordinary football. Alerting here would fire on matches that routinely turn, which is the
    failure mode that makes an early-warning feature worthless."""
    assert _state(home_score=0, away_score=1, match_minute=30) is PickState.ON_TRACK


def test_one_goal_down_late_is_at_risk() -> None:
    assert _state(home_score=0, away_score=1, match_minute=70) is PickState.AT_RISK


def test_two_goals_down_is_at_risk_even_early() -> None:
    """The margin shortcut: two goals is a different situation from one, whatever the clock."""
    assert _state(home_score=0, away_score=2, match_minute=20) is PickState.AT_RISK


def test_the_away_rule_mirrors_the_home_rule() -> None:
    assert (
        _state(selection="away", home_score=1, away_score=0, match_minute=70) is PickState.AT_RISK
    )
    assert (
        _state(selection="away", home_score=0, away_score=1, match_minute=70) is PickState.ON_TRACK
    )


def test_a_draw_pick_is_threatened_by_either_side_leading() -> None:
    assert _state(selection="draw", home_score=0, away_score=0) is PickState.ON_TRACK
    assert (
        _state(selection="draw", home_score=1, away_score=0, match_minute=75) is PickState.AT_RISK
    )
    assert (
        _state(selection="draw", home_score=0, away_score=1, match_minute=75) is PickState.AT_RISK
    )
    assert (
        _state(selection="draw", home_score=2, away_score=0, match_minute=20) is PickState.AT_RISK
    )


# --- football double chance --------------------------------------------------------------


def test_double_chance_is_more_forgiving_than_the_matching_h2h() -> None:
    """THE POINT OF THE MARKET: a draw still wins it, so the same scoreline is genuinely less
    threatening. At 70' one down, h2h:home is at risk and 1X is not."""
    behind = dict(home_score=0, away_score=1, match_minute=70)

    assert _state(market="h2h", selection="home", **behind) is PickState.AT_RISK
    assert _state(market="double_chance", selection="1X", **behind) is PickState.ON_TRACK


def test_double_chance_does_become_at_risk_late() -> None:
    assert (
        _state(market="double_chance", selection="1X", home_score=0, away_score=1, match_minute=75)
        is PickState.AT_RISK
    )
    assert (
        _state(market="double_chance", selection="X2", home_score=1, away_score=0, match_minute=80)
        is PickState.AT_RISK
    )


# --- football goals ----------------------------------------------------------------------


def test_under_is_the_one_market_that_can_be_lost_before_full_time() -> None:
    """Goals never come off the board, so this is arithmetic rather than a forecast."""
    assert (
        _state(market="goals_total", selection="under", line=2.5, home_score=2, away_score=1)
        is PickState.LOST
    )


def test_under_is_at_risk_one_goal_short_of_dead() -> None:
    assert (
        _state(
            market="goals_total",
            selection="under",
            line=2.5,
            home_score=1,
            away_score=1,
            match_minute=60,
        )
        is PickState.AT_RISK
    )


def test_under_stops_warning_once_the_warning_is_useless() -> None:
    """Accurate and unactionable is the failure this feature exists to avoid — past 75' the user
    can do nothing with it, so the state reverts to ON TRACK rather than nagging."""
    assert (
        _state(
            market="goals_total",
            selection="under",
            line=2.5,
            home_score=1,
            away_score=1,
            match_minute=80,
        )
        is PickState.ON_TRACK
    )


def test_over_uses_two_thresholds_trading_shortfall_against_time() -> None:
    """One goal short is only alarming late; two goals short is alarming a good deal earlier."""
    one_short = dict(market="goals_total", selection="over", line=2.5, home_score=1, away_score=1)
    assert _state(**one_short, match_minute=60) is PickState.ON_TRACK
    assert _state(**one_short, match_minute=70) is PickState.AT_RISK

    two_short = dict(market="goals_total", selection="over", line=2.5, home_score=0, away_score=0)
    assert _state(**two_short, match_minute=50) is PickState.ON_TRACK
    assert _state(**two_short, match_minute=55) is PickState.AT_RISK


def test_over_already_cleared_is_on_track() -> None:
    assert (
        _state(market="goals_total", selection="over", line=2.5, home_score=2, away_score=1)
        is PickState.ON_TRACK
    )


# --- tennis ------------------------------------------------------------------------------


def test_tennis_reads_completed_sets_and_nothing_finer() -> None:
    """Scores are completed SETS, so this is a late signal and is documented as one."""
    losing = dict(sport_slug="tennis", home_score=0, away_score=1, match_minute=None)
    assert _state(**losing) is PickState.AT_RISK
    assert _state(sport_slug="tennis", home_score=1, away_score=0, match_minute=None) is (
        PickState.ON_TRACK
    )
    assert _state(sport_slug="tennis", home_score=0, away_score=0, match_minute=None) is (
        PickState.ON_TRACK
    )


# --- alerting is narrower than tagging ----------------------------------------------------


@pytest.mark.parametrize("state", [PickState.ON_TRACK, PickState.UNKNOWN])
def test_only_at_risk_is_worth_a_push(state: PickState) -> None:
    assert should_alert(state, sport_slug="football", match_minute=60) is False


def test_lost_is_deliberately_never_alerted() -> None:
    """By then there is nothing to act on, and a notification that only confirms a loss is a
    worse product than silence."""
    assert should_alert(PickState.LOST, sport_slug="football", match_minute=60) is False


def test_no_football_alert_in_the_closing_minutes() -> None:
    """The card may still show AT RISK — this bound is about waking a phone, not displaying."""
    assert should_alert(PickState.AT_RISK, sport_slug="football", match_minute=85) is True
    assert should_alert(PickState.AT_RISK, sport_slug="football", match_minute=86) is False
