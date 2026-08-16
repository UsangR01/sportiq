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
    for slug in ("veikkausliiga", "czech_first", "ekstraklasa", "liga_i"):
        assert not offers_corners(slug), slug


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
