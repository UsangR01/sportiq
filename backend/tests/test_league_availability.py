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


# === The other three operations the runbook documents (docs/suppressing-leagues-and-markets.md)


def test_the_include_history_set_is_empty_by_default():
    """It hides a league's SETTLED cards too, which deletes its losses from view and makes the
    visible record better than reality. That is a deliberate, narrow tool (a league ingested by
    mistake, corrupt data, a retirement) and a bad default, so it ships empty and the ordinary
    suppression above is what "this league is performing badly" should use."""
    from app.fixtures.league_availability import SUPPRESSED_LEAGUES_INCLUDING_HISTORY

    assert SUPPRESSED_LEAGUES_INCLUDING_HISTORY == frozenset()


def test_the_stronger_set_also_withholds_picks(monkeypatch):
    monkeypatch.setattr(
        "app.fixtures.league_availability.SUPPRESSED_LEAGUES_INCLUDING_HISTORY",
        frozenset({"csl"}),
    )
    assert offers_picks("csl") is False
    assert offers_picks("epl") is True


def test_the_history_hiding_filter_is_unconditional_on_status():
    """The whole difference from SUPPRESSED_LEAGUES: no COMPLETED escape hatch. Pinned by
    reading the query so the two filters cannot silently converge."""
    source = (Path(__file__).resolve().parents[1] / "app" / "fixtures" / "router.py").read_text(
        encoding="utf-8"
    )
    listing = source[source.index("async def list_fixtures") :]
    start = listing.index("if not league_slug and SUPPRESSED_LEAGUES_INCLUDING_HISTORY:")
    guard = listing[start : listing.index("if sport_slug:", start)]
    assert "not_in" in guard
    assert "FixtureStatus.COMPLETED" not in guard, "this set must NOT exempt settled fixtures"


def test_no_market_is_suppressed_per_league_by_default():
    from app.fixtures.league_availability import SUPPRESSED_MARKETS_BY_LEAGUE

    assert SUPPRESSED_MARKETS_BY_LEAGUE == {}


def test_a_market_can_be_withheld_for_one_league_only(monkeypatch):
    """Operation 2 in the runbook, and the one no existing gate could express: h2h and
    double_chance have no measured per-league gate at all."""
    from app.fixtures.league_availability import suppressed_markets_for

    monkeypatch.setattr(
        "app.fixtures.league_availability.SUPPRESSED_MARKETS_BY_LEAGUE",
        {"csl": frozenset({"double_chance"})},
    )
    assert suppressed_markets_for("csl") == {"double_chance"}
    assert suppressed_markets_for("epl") == frozenset()
    assert suppressed_markets_for(None) == frozenset()


def test_the_operator_override_is_applied_to_the_candidate_list():
    """Pinned by source order, the same way the goals and corners gates are: the behaviour
    lives in one branch of one function and a silent removal would be invisible otherwise."""
    source = (Path(__file__).resolve().parents[1] / "app" / "fixtures" / "router.py").read_text(
        encoding="utf-8"
    )
    picks = source[source.index("async def _bulk_best_picks") :]
    body = picks[: picks.index("return best_picks")]
    assert "suppressed_markets_for(" in body
    assert body.index("offers_corners(") < body.index("suppressed_markets_for(")
    assert body.index("suppressed_markets_for(") < body.index("_pick_best(")
