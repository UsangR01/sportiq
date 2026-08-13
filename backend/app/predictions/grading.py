"""Was a SHOWN pick right? The four-market rule, in Python.

WHY THIS EXISTS. Until now this rule lived exactly once, in TypeScript
(mobile/lib/pickFormat.ts:evaluatePickCorrectness), so only the phone could say whether a card
had been right. Everything that MEASURED the product — /history, /history/summary and the
analytics notebook — graded something else entirely: the 1X2 argmax of the stored Prediction
row. Those are different objects, and the gap is not academic. The live feed's headline picks
run roughly 50% corners / 33% h2h / 18% double chance, so the published accuracy describes a
market that wins about a third of the cards a user actually sees.

The case that surfaced it: Hearts v Dundee Utd, 2026-08-09. The card showed UNDER 10.5 CORNERS
at 2.05 and the match finished 7-2 on corners, so the card was RIGHT. The same fixture's 1X2
call was away at 0.383 against a 4-0 home win, so the history table recorded it as WRONG. Both
were accurate about their own object; only one of them is what the product promised.

PARITY WITH THE PHONE IS THE POINT, so this is a deliberate line-by-line port rather than an
improvement, including the parts that look like omissions:
  - a "12" double chance returns None, not a graded result, because the phone does not handle
    it either. Diverging here would mean the card and the measurement disagree, which is the
    defect this module exists to close.
  - strict inequalities on BOTH sides of a total, so an exact push grades False either way
    rather than True. Every line in use is a .5 line (GOALS_LINES/CORNERS_LINES), so this
    cannot currently fire.
  - None means UNVERIFIABLE and never "wrong". Corners are null for NBA and for any fixture
    settled before fixture_live_state gained its corner columns; scoring those as losses would
    invent a losing record out of missing data.

test_pick_grading.py pins the port against the TypeScript source. A change to one without the
other is the failure mode worth catching.
"""

from __future__ import annotations

# Retired/walkover. Most bookmakers void rather than settle these, so the card shows a neutral
# "VOID" badge instead of a tick or a cross, and a measurement of shown picks must drop them
# rather than count them either way. Mirrors FixtureCard.tsx, which skips grading entirely when
# fixture_live_state.result_type is set.
VOIDING_RESULT_TYPES = frozenset({"retired", "walkover"})


def actual_outcome(home_score: int, away_score: int) -> str:
    """ "home" | "draw" | "away" from the final score."""
    if home_score > away_score:
        return "home"
    if home_score < away_score:
        return "away"
    return "draw"


def grade_pick(
    market: str,
    selection: str,
    line: float | None,
    home_score: int | None,
    away_score: int | None,
    home_corners: int | None = None,
    away_corners: int | None = None,
    result_type: str | None = None,
) -> bool | None:
    """True/False if the shown pick can be graded, None when it genuinely cannot.

    Port of mobile/lib/pickFormat.ts:evaluatePickCorrectness — see the module docstring for why
    the divergences are deliberate. result_type is an addition rather than a divergence: the
    phone applies the same void rule one level up, in FixtureCard.tsx.
    """
    if home_score is None or away_score is None:
        return None
    if result_type in VOIDING_RESULT_TYPES:
        return None

    actual = actual_outcome(home_score, away_score)

    if market == "h2h":
        return selection == actual
    if market == "double_chance":
        if selection == "1X":
            return actual in ("home", "draw")
        if selection == "X2":
            return actual in ("away", "draw")
        return None
    if market in ("goals_total", "corners_total"):
        if line is None:
            return None
        if market == "goals_total":
            total: float | None = home_score + away_score
        else:
            if home_corners is None or away_corners is None:
                return None
            total = home_corners + away_corners
        if selection == "over":
            return total > line
        if selection == "under":
            return total < line
        return None
    return None
