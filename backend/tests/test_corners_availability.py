"""Do not offer a market you cannot settle.

A corners pick can only show a tick or a cross once we hold the corner counts. API-Football is
the only source that publishes them promptly; TheStatsAPI covers far more leagues but lags about
twelve hours -- measured 2026-08-14: 404 at 3.4h past kickoff, nothing at 7.9h, present at
12.9h. So in a league API-Football does not cover, a corners pick sits GREY through the whole
window in which users open the app to see whether they won, and no retry cadence changes that.

Measured per league against API-Football directly, 2026-08-16:

    veikkausliiga     0/6    0%
    czech_first       2/6   33%
    ekstraklasa       3/6   50%
    liga_i            4/6   67%
    brasileirao       5/6   83%   kept
    everything else   6/6  100%

This is a data-supply limitation, not a verdict on the corners model, which measures a real if
modest signal (r=+0.288 against actual totals, versus goals' +0.049). The exclusion set is
built to be emptied in one edit the day a richer provider lands.
"""

import ast
from pathlib import Path

from app.fixtures.corners_availability import (
    MIN_PROMPT_CORNER_COVERAGE,
    offers_corners,
)


def test_leagues_that_cannot_settle_corners_do_not_offer_them():
    """Membership tracks the MEASUREMENT (scripts/measure_prompt_corner_coverage.py), not a
    historical snapshot: czech_first and ekstraklasa were re-admitted 2026-08-19 after
    re-measuring 6/6 prompt, and uel joined at 1/6 (qualifier clubs without prompt stats)."""
    for slug in ("veikkausliiga", "liga_i", "uel"):
        assert not offers_corners(slug), slug
    for slug in ("czech_first", "ekstraklasa", "ucl"):
        assert offers_corners(slug), slug


def test_leagues_that_settle_promptly_still_offer_corners():
    for slug in ("epl", "mls", "j1_league", "brasileirao", "scottish_prem", "csl"):
        assert offers_corners(slug), slug


def test_an_unknown_league_still_offers_corners():
    """A newly-added competition has no measurement yet. Withholding a market by default would
    be worse than showing it and finding out -- the measurement script is what moves a league
    into the set."""
    assert offers_corners("a_league_added_next_week")
    assert offers_corners(None)


def test_emptying_the_set_restores_corners_everywhere(monkeypatch):
    """THE RE-ENABLING PATH, exercised rather than described. The day a provider with prompt
    corners across every league is in place, this one edit is the whole change."""
    monkeypatch.setattr(
        "app.fixtures.corners_availability.LEAGUES_WITHOUT_PROMPT_CORNERS", frozenset()
    )
    for slug in ("veikkausliiga", "czech_first", "ekstraklasa", "liga_i"):
        assert offers_corners(slug), slug


def test_the_threshold_admits_the_occasional_missed_fixture():
    """0.80 rather than 1.00 on purpose: a provider missing one fixture in six is normal and the
    15-minute second-source fill covers it within the day. What this excludes is leagues where a
    MAJORITY of cards would sit grey overnight -- brasileirao at 83% is kept for exactly that
    reason."""
    assert 0.5 < MIN_PROMPT_CORNER_COVERAGE < 1.0
    assert offers_corners("brasileirao")


def test_the_measurement_script_reads_the_same_constants():
    """The script exists so the set is re-derived from data rather than remembered. If it stops
    importing these, the two drift and the comment becomes folklore."""
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "measure_prompt_corner_coverage.py"
    ).read_text(encoding="utf-8")
    imported = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module == "app.fixtures.corners_availability"
        for alias in node.names
    }
    assert {"LEAGUES_WITHOUT_PROMPT_CORNERS", "MIN_PROMPT_CORNER_COVERAGE"} <= imported


def test_the_conference_league_is_barred_for_absent_skill_not_supply():
    """UECL settles corners promptly (6/6 measured) but its corners predictions carry no
    signal: near-constant ~11 predicted in a ~9.4-corner competition live, r=+0.049 with a
    zero-straddling CI on the held-out split, 5/13 over-9.5 picks won. Scoped to uecl alone by
    explicit product decision — ucl keeps the market, and improving settlement supply must
    never re-admit uecl on its own."""
    from app.fixtures.corners_availability import (
        LEAGUES_WITHOUT_DEMONSTRATED_CORNERS_SIGNAL,
        LEAGUES_WITHOUT_PROMPT_CORNERS,
    )

    assert LEAGUES_WITHOUT_DEMONSTRATED_CORNERS_SIGNAL == {"uecl"}
    assert not offers_corners("uecl")
    assert offers_corners("ucl")
    assert "uecl" not in LEAGUES_WITHOUT_PROMPT_CORNERS


# === A side of a line, not a league (2026-08-24) ==================================================


def test_over_9_5_is_barred_from_the_headline():
    """THE MEASUREMENT. The model is calibrated on average and overconfident exactly where picks
    are drawn from — on the served model's own test parquet, P(over 9.5) claimed 0.634 against
    an actual 0.566 at predicted >= 0.60, and 0.736 against 0.600 at >= 0.70. The gap GROWS with
    confidence, and a pick must clear ~0.5955 to reach a card at all.

    Confirmed live: 69 settled over-9.5 picks hit 52% against a claimed 67%, with the claim
    outside the interval's upper bound of 64%. 52% is the pooled base rate, so they added
    nothing.
    """
    from app.fixtures.corners_availability import corner_selection_is_publishable

    assert corner_selection_is_publishable("over", 9.5) is False


def test_under_10_5_is_deliberately_untouched():
    """Why this is a side-of-a-line rule and not "corners are broken": 18 settled under-10.5
    picks hit 72% against a claimed 70%. Barring the whole market would have deleted a
    well-calibrated one along with the bad one."""
    from app.fixtures.corners_availability import corner_selection_is_publishable

    assert corner_selection_is_publishable("under", 10.5) is True
    assert corner_selection_is_publishable("over", 10.5) is True
    assert corner_selection_is_publishable("under", 9.5) is True


def test_a_line_less_candidate_is_never_barred():
    """Defensive: every corners candidate carries a line, but a None must not be silently
    treated as a barred one."""
    from app.fixtures.corners_availability import corner_selection_is_publishable

    assert corner_selection_is_publishable("over", None) is True


def test_the_bar_is_applied_where_the_headline_is_chosen():
    """Pinned by source order like the other two gates: the behaviour lives in one branch of one
    function and a silent removal would be invisible in the response."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "fixtures" / "router.py").read_text(
        encoding="utf-8"
    )
    picks = source[source.index("async def _bulk_best_picks") :]
    body = picks[: picks.index("return best_picks")]
    assert "corner_selection_is_publishable(" in body
    assert body.index("offers_corners(") < body.index("corner_selection_is_publishable(")
    assert body.index("corner_selection_is_publishable(") < body.index("_pick_best(")
