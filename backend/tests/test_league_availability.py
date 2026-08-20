"""A league can be withheld from the feed entirely — and must not take its own history with it.

MLS was suppressed 2026-08-19 on a direct product decision after roughly 4 of 11 settled card
picks landed. The tests that matter here are the SCOPE ones: hiding a league's undecided
fixtures is a product choice, while hiding its settled ones would delete the very losses that
prompted the suppression and make the visible track record better than reality.
"""

import ast
from pathlib import Path

from app.fixtures.league_availability import SUPPRESSED_LEAGUES, offers_picks


def test_mls_is_suppressed():
    assert "mls" in SUPPRESSED_LEAGUES
    assert offers_picks("mls") is False


def test_every_other_league_is_untouched():
    for slug in ("epl", "laliga", "seriea", "ligue1", "bundesliga", "j1_league", "ucl", "uecl"):
        assert offers_picks(slug) is True, slug


def test_an_unmeasured_league_keeps_working():
    """The OPPOSITE polarity to offers_goals, deliberately. This is an explicit denylist of
    leagues somebody decided to withhold, so a league nobody has ruled on is unaffected —
    defaulting to suppressed would silently hide every new competition on the day it is added."""
    assert offers_picks("a_league_added_next_week") is True
    assert offers_picks(None) is True


def test_the_feed_hides_only_undecided_fixtures_of_a_suppressed_league():
    """THE LOAD-BEARING SCOPE TEST, pinned by reading the query rather than by trusting a
    comment: the filter must be disjunctive with COMPLETED, so settled cards survive it.

    Hiding settled fixtures too would improve the reported record by deleting losses — the
    same retroactive-filtering bias that made a raised completeness floor erase a published,
    winning Hearts v Dundee Utd pick (see CLAUDE.md)."""
    source = (Path(__file__).resolve().parents[1] / "app" / "fixtures" / "router.py").read_text(
        encoding="utf-8"
    )
    listing = source[source.index("async def list_fixtures") :]
    guard = listing[listing.index("SUPPRESSED_LEAGUES") : listing.index("if sport_slug:")]
    assert "or_(" in guard, "the suppression filter must not be unconditional"
    assert "FixtureStatus.COMPLETED" in guard, "settled fixtures must be exempt from hiding"
    assert "not_in" in guard


def test_an_explicit_league_request_still_serves_a_suppressed_league():
    """Suppression governs the DEFAULT feed, not the API. A deep link, a direct
    ?league_slug=mls query, or the history endpoints must keep working — otherwise this stops
    being a display decision and starts being data loss."""
    source = (Path(__file__).resolve().parents[1] / "app" / "fixtures" / "router.py").read_text(
        encoding="utf-8"
    )
    listing = source[source.index("async def list_fixtures") :]
    guard_line = listing[listing.index("if not league_slug and SUPPRESSED_LEAGUES") :][:120]
    assert "not league_slug" in guard_line


def test_history_does_not_import_the_suppression_list():
    """MLS must keep counting toward every accuracy this project reports. If /history ever
    starts filtering on this list, the product would be grading itself on a population it chose
    after seeing the results."""
    source = (Path(__file__).resolve().parents[1] / "app" / "history" / "router.py").read_text(
        encoding="utf-8"
    )
    imported_modules = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "app.fixtures.league_availability" not in imported_modules


def test_emptying_the_set_restores_the_league(monkeypatch):
    """The lift path, exercised rather than described — deleting the slug is the whole change."""
    monkeypatch.setattr("app.fixtures.league_availability.SUPPRESSED_LEAGUES", frozenset())
    assert offers_picks("mls") is True
