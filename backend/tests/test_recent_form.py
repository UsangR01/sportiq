"""The W/D/L run shown on the fixture detail screen.

Asked for as "aside the Head to Head Stats we already capture, I want to also capture the
Streak of the team (team sport) or player (individual sport)".

A SEQUENCE rather than the count TeamStats.win_streak already carried: "three in a row" cannot
tell WWWLL from WWWWW, and those are not the same side going into a match.

ORDER IS THE WHOLE RISK HERE. API-Football's form string is oldest-first, the two BallDontLie
sports produce newest-first, and a silently reversed run would read as confidently as a correct
one -- there is no error and no gap, just a wrong answer to the first question anyone asks.
"""

from app.adapters.api_football import RECENT_FORM_LENGTH, _parse_streaks, _recent_form
from app.adapters.balldontlie import _compute_team_stats as nba_team_stats


def _nba_game(index: int, *, won: bool) -> dict:
    return {
        "status": "Final",
        # Ascending dates; _compute_team_stats sorts descending itself, which is what makes
        # this a real test of the ordering rather than of the input order.
        "datetime": f"2026-08-{10 + index:02d}T00:00:00Z",
        "home_team": {"id": "1"},
        "visitor_team": {"id": "2"},
        "home_team_score": 110 if won else 90,
        "visitor_team_score": 100,
    }


def test_football_form_is_reversed_because_the_provider_sends_it_oldest_first() -> None:
    """API-Football's "WWDLW" ends with the MOST RECENT match. The card reads left to right as
    newest to oldest, so the last character must come out first."""
    assert _recent_form("WWDLW") == "WLDWW"


def test_only_the_most_recent_results_are_kept() -> None:
    form = _recent_form("WWWWWWWWLL")
    assert form is not None
    assert len(form) == RECENT_FORM_LENGTH
    # The two losses are the newest, so they lead.
    assert form.startswith("LL")


def test_a_short_run_is_kept_short_rather_than_padded() -> None:
    """Measured live on the day this was built: EPL was one match into its season, so Crystal
    Palace genuinely had a single result. One chip is the honest answer; padding to five would
    invent four matches that have not been played."""
    assert _recent_form("L") == "L"


def test_nothing_to_report_is_none_not_an_empty_string() -> None:
    """None lets the client say "no results yet" instead of rendering an empty gap that reads
    as a rendering fault."""
    assert _recent_form(None) is None
    assert _recent_form("") is None


def test_characters_that_are_not_a_result_are_dropped() -> None:
    """The field occasionally carries something other than W/D/L for an awarded or unplayed
    match. A chip we cannot label is worse than a shorter run."""
    assert _recent_form("W-DL") == "LDW"


def test_basketball_reads_newest_first_from_its_own_game_list() -> None:
    """The other direction: BallDontLie returns games that this adapter sorts newest-first
    itself, so no reversal is wanted -- and applying one anyway would be invisible."""
    games = [_nba_game(0, won=True), _nba_game(1, won=True), _nba_game(2, won=False)]

    stats = nba_team_stats("1", games, 10)

    # 2026-08-12 is the newest and was a loss.
    assert stats.recent_form == "LWW"


def test_basketball_never_reports_a_draw() -> None:
    stats = nba_team_stats("1", [_nba_game(i, won=i % 2 == 0) for i in range(5)], 10)
    assert stats.recent_form is not None
    assert set(stats.recent_form) <= {"W", "L"}


def test_the_run_agrees_with_the_streak_counter_it_sits_beside() -> None:
    """Two readings of the same matches must not contradict each other on one card.

    Checked on FOOTBALL because that is where both come from the same source string and where
    a counter actually exists -- the basketball adapter reports no streak at all today. A
    three-win streak has to show exactly three leading Ws, and a draw at the newest match has
    to break both counters while still appearing as a chip.
    """
    wins, losses = _parse_streaks("LLWWW")
    assert (wins, losses) == (3.0, 0.0)
    assert _recent_form("LLWWW") == "WWWLL"

    wins, losses = _parse_streaks("WWWWD")
    assert (wins, losses) == (0.0, 0.0)
    assert _recent_form("WWWWD") == "DWWWW"
