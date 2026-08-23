"""Is a pick currently landing, currently threatened, or already gone?

ONE RULE SET, TWO SURFACES. The `ON TRACK` / `AT RISK` tag on the Live card and the push alert
for a saved pick are the same evaluation. Written as pure functions over a scoreline so they can
be tested without a database, a provider or a worker -- and so the badge and the notification can
never disagree, which they would within a week if each had its own copy.

WHAT CAN AND CANNOT BE EVALUATED, MEASURED RATHER THAN ASSUMED:

    football     match_minute populated on 332/332 live rows   -> full support
    tennis       minute and period on 0/2,366 rows             -> completed sets only
    basketball   minute and period on 0/263 rows               -> nothing at all
    corners      home_corners written once, AT SETTLEMENT      -> no in-play state exists

UNKNOWN IS A FIRST-CLASS ANSWER AND MUST STAY VISIBLE AS ONE. A market with no rule renders no
tag rather than a neutral one: an absent tag reads as "not applicable", a grey tag reads as "we
checked and it is fine". The same applies to the progress bar, which takes its colour from this
-- a green bar on a corners pick would claim a fact we do not have.
"""

from __future__ import annotations

import enum
import math


class PickState(str, enum.Enum):
    """How a pick is doing RIGHT NOW. Not a prediction -- a reading of the current score."""

    #: Currently landing.
    ON_TRACK = "on_track"
    #: Currently failing AND recovery is materially threatened. The only state worth a push.
    AT_RISK = "at_risk"
    #: Arithmetically decided against before full time. Deliberately NOT alerted on: there is
    #: nothing left to act on, and a notification that only confirms a loss is worse than
    #: silence.
    LOST = "lost"
    #: No rule applies, or the data needed to judge it does not exist. Never rendered as a tag.
    UNKNOWN = "unknown"


#: Football. Past this point an alert cannot be acted on, so it is noise wearing a premium label.
#: The TAG still updates -- this bound is about pushing, not about displaying.
ALERT_SUPPRESSED_AFTER_MINUTE = 85

#: Sports whose live payload carries no clock at all. A trailing scoreline cannot be placed in
#: the game -- a six-point deficit in the first quarter and the same with a minute left are
#: indistinguishable to us, and basketball leads swing far too much for a margin-only rule.
_SPORTS_WITHOUT_A_CLOCK = frozenset({"nba"})

#: Written once at settlement rather than during play, so there is no in-play state to read.
#: Showing a guessed tag here would be inventing a fact.
_MARKETS_WITHOUT_LIVE_STATE = frozenset({"corners_total"})


def evaluate(
    *,
    sport_slug: str,
    market: str,
    selection: str,
    line: float | None,
    home_score: int | None,
    away_score: int | None,
    match_minute: int | None,
) -> PickState:
    """The current state of one pick. Returns UNKNOWN rather than guessing."""
    if home_score is None or away_score is None:
        return PickState.UNKNOWN
    if market in _MARKETS_WITHOUT_LIVE_STATE:
        return PickState.UNKNOWN
    if sport_slug in _SPORTS_WITHOUT_A_CLOCK:
        return PickState.UNKNOWN

    if sport_slug == "tennis":
        return _tennis(selection, home_score, away_score)
    if sport_slug != "football":
        return PickState.UNKNOWN

    # Every football rule below needs the clock. It is populated on every real row, but a
    # fixture polled between kick-off and the provider's first update can still lack it.
    if match_minute is None:
        return PickState.UNKNOWN

    if market == "h2h":
        return _football_h2h(selection, home_score, away_score, match_minute)
    if market == "double_chance":
        return _football_double_chance(selection, home_score, away_score, match_minute)
    if market == "goals_total" and line is not None:
        return _football_goals_total(selection, home_score + away_score, line, match_minute)
    return PickState.UNKNOWN


def _football_h2h(selection: str, home: int, away: int, minute: int) -> PickState:
    """A pick is threatened once it is BEHIND and the game is either late or the gap is real.

    Minute 70 or a two-goal margin, because one goal with half an hour left is ordinary football
    and alerting on it would fire on matches that routinely turn.
    """
    if selection == "draw":
        margin = abs(home - away)
        if margin == 0:
            return PickState.ON_TRACK
        return PickState.AT_RISK if (minute >= 75 or margin >= 2) else PickState.ON_TRACK

    if selection == "home":
        deficit = away - home
    elif selection == "away":
        deficit = home - away
    else:
        return PickState.UNKNOWN

    if deficit <= 0:
        return PickState.ON_TRACK
    return PickState.AT_RISK if (minute >= 70 or deficit >= 2) else PickState.ON_TRACK


def _football_double_chance(selection: str, home: int, away: int, minute: int) -> PickState:
    """Deliberately more forgiving than the matching h2h: a draw still wins this, so the same
    scoreline is genuinely less threatening. Hence minute 75 and no margin shortcut."""
    if selection == "1X":
        losing = away > home
    elif selection == "X2":
        losing = home > away
    else:
        return PickState.UNKNOWN

    if not losing:
        return PickState.ON_TRACK
    return PickState.AT_RISK if minute >= 75 else PickState.ON_TRACK


def _football_goals_total(selection: str, total: int, line: float, minute: int) -> PickState:
    """UNDER is the one market that can be LOST outright before full time, because goals never
    come off the board. That makes its early warning certain rather than probabilistic, and the
    most valuable alert this feature has."""
    if selection == "under":
        if total > line:
            return PickState.LOST
        # One more goal kills it. Bounded to the first 75 minutes: after that the warning is
        # accurate and useless, which is the failure mode this whole feature is trying to avoid.
        if total > line - 1 and minute <= 75:
            return PickState.AT_RISK
        return PickState.ON_TRACK

    if selection == "over":
        # GOALS ACTUALLY NEEDED, not the raw difference -- a deliberate departure from the
        # spec's table, which reads "N - total >= 1". Every line this product sells is a HALF
        # line, so a pick one goal short of landing gives N - total = 0.5 and the literal rule
        # could never fire for it. That would make the most common at-risk case for `over`
        # permanently silent, which is the opposite of the table's stated intent.
        needed = math.ceil(line - total)
        if needed <= 0:
            return PickState.ON_TRACK
        # Two thresholds because shortfall and time remaining trade off: one goal short is only
        # alarming late, two goals short is alarming a good deal earlier.
        if (needed >= 1 and minute >= 70) or (needed >= 2 and minute >= 55):
            return PickState.AT_RISK
        return PickState.ON_TRACK

    return PickState.UNKNOWN


def _tennis(selection: str, home_sets: int, away_sets: int) -> PickState:
    """The only rule the data supports, and it is a late one.

    Scores here are COMPLETED SETS, so the earliest observable signal is "the opponent has won a
    set" -- by which point a best-of-three is already close to decided. Shipped because it is
    honest and better than nothing; not marketed as early warning until the provider's per-match
    set_scores array is ingested, which would give games within the current set.
    """
    if selection == "home":
        behind = away_sets > home_sets
    elif selection == "away":
        behind = home_sets > away_sets
    else:
        return PickState.UNKNOWN
    return PickState.AT_RISK if behind else PickState.ON_TRACK


def should_alert(state: PickState, *, sport_slug: str, match_minute: int | None) -> bool:
    """Whether a transition into `state` is worth waking someone's phone for.

    Deliberately narrower than the tag: the card can show AT RISK all match, while a push has to
    arrive early enough to act on and only once.
    """
    if state is not PickState.AT_RISK:
        return False
    if sport_slug == "football":
        if match_minute is None:
            return False
        return match_minute <= ALERT_SUPPRESSED_AFTER_MINUTE
    return True
