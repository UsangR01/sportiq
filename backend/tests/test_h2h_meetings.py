"""Past meetings now render for tennis and basketball, and ORIENTATION is the whole risk.

The data was always there -- every adapter fetched these games to compute the record and then
discarded them -- so this was a mapping gap, not a data one. What makes it worth testing is the
thing that makes it readable: `result` is W/L from THE CURRENT FIXTURE'S home side, while
home_team/away_team keep the historical meeting's own orientation. Get that backwards and half
of all rows invert, silently and plausibly.

For tennis it is sharper still. BallDontLie answers in its own player1/player2 order, which is
NOT our home/away -- the same class of error as trusting TheRundown's teams_normalized order,
which this repo has already been bitten by.
"""

from datetime import UTC, datetime

from app.adapters.balldontlie_tennis import _meeting_rows


def _match(*, mid, p1, p2, sets, tournament="Test Open"):
    """A match in the provider's OWN shape, where player1 is not necessarily our home side."""
    return {
        "id": mid,
        "player1": {"id": p1, "full_name": f"P{p1}"},
        "player2": {"id": p2, "full_name": f"P{p2}"},
        "scheduled_time": datetime(2026, 5, 1, 12, 0, tzinfo=UTC).isoformat(),
        "set_scores": sets,
        "match_status": "finished",
        "tournament": {"name": tournament, "start_date": "2026-05-01"},
    }


def test_result_is_scored_from_the_current_home_player_not_the_provider_slot():
    """The same match, read from each player's side, must give OPPOSITE verdicts.

    This is the assertion that would fail if the mapping trusted player1/player2 position.
    """
    match = _match(
        mid=1,
        p1=10,
        p2=20,
        sets=[{"player1_games": 6, "player2_games": 4}, {"player1_games": 6, "player2_games": 3}],
    )

    as_ten = _meeting_rows([match], "10")
    as_twenty = _meeting_rows([match], "20")

    assert len(as_ten) == 1 and len(as_twenty) == 1
    assert as_ten[0].result != as_twenty[0].result, "verdict did not flip with perspective"
    assert {as_ten[0].result, as_twenty[0].result} == {"W", "L"}


def test_the_displayed_scoreline_does_not_flip_with_perspective():
    """Only the VERDICT is perspective-relative. The row shows the match as it was played, so
    the same meeting must read identically for both players apart from W/L."""
    match = _match(
        mid=2,
        p1=10,
        p2=20,
        sets=[{"player1_games": 6, "player2_games": 4}, {"player1_games": 6, "player2_games": 3}],
    )

    a, b = _meeting_rows([match], "10")[0], _meeting_rows([match], "20")[0]

    assert (a.home_team, a.away_team) == (b.home_team, b.away_team)
    assert (a.home_score, a.away_score) == (b.home_score, b.away_score)


def test_a_retirement_with_equal_sets_is_omitted_rather_than_shown_as_a_draw():
    """Tennis cannot draw, so a tied set count has no honest W/L.

    A real example from this repo's history is `6-1, 6-7, 0-2 ret.` -- 1-1 in COMPLETED sets
    with a real winner that the score alone cannot name. Showing it as a draw once made a
    correct prediction render as a red cross, so the row is dropped instead. The meeting still
    counts toward the record, which comes from the provider's own H2H endpoint.
    """
    tied = _match(
        mid=3,
        p1=10,
        p2=20,
        sets=[{"player1_games": 6, "player2_games": 1}, {"player1_games": 6, "player2_games": 7}],
    )

    assert _meeting_rows([tied], "10") == []


def test_the_tournament_name_is_carried_so_no_row_renders_a_blank_label():
    """The reason the schema used to say football-only: tennis and basketball were assumed not
    to carry a competition name. They do."""
    match = _match(
        mid=4,
        p1=10,
        p2=20,
        sets=[{"player1_games": 6, "player2_games": 4}, {"player1_games": 6, "player2_games": 2}],
        tournament="Winston-Salem Open",
    )

    assert _meeting_rows([match], "10")[0].competition == "Winston-Salem Open"
