"""Head-to-head panels for basketball and tennis, and why they are different depths.

Football's panel has five rows because API-Football hands over corners, shots and possession.
The other two are shaped by what their provider actually allows, probed live 2026-08-13:

    tennis     /head_to_head gives the record outright; /match_stats gives serve and return
    NBA/WNBA   /stats returns 401 on this plan, so the final score is the only real
               per-meeting number that exists

Two rows for basketball is the data, not a shortcut. Inventing rebounds or field-goal
percentage would mean fabricating them.
"""

import pytest

from app.adapters.balldontlie import fetch_h2h_panel as basketball_panel

NBA_MEETINGS = [
    # our home team (id 5) at home, won 110-100
    {
        "id": 1,
        "datetime": "2026-01-10T00:00:00Z",
        "status": "Final",
        "home_team": {"id": 5},
        "visitor_team": {"id": 9},
        "home_team_score": 110,
        "visitor_team_score": 100,
    },
    # our home team AWAY, lost 90-95 -- the perspective flip that must not invert the record
    {
        "id": 2,
        "datetime": "2026-02-10T00:00:00Z",
        "status": "Final",
        "home_team": {"id": 9},
        "visitor_team": {"id": 5},
        "home_team_score": 95,
        "visitor_team_score": 90,
    },
    # a game against someone else entirely -- must not be counted
    {
        "id": 3,
        "datetime": "2026-03-10T00:00:00Z",
        "status": "Final",
        "home_team": {"id": 5},
        "visitor_team": {"id": 77},
        "home_team_score": 120,
        "visitor_team_score": 80,
    },
]


@pytest.fixture
def fake_games(monkeypatch):
    async def _fake_fetch_all_games(client, params):
        return NBA_MEETINGS

    monkeypatch.setattr("app.adapters.balldontlie._fetch_all_games", _fake_fetch_all_games)


async def test_basketball_record_is_from_our_fixtures_perspective(fake_games):
    """One win at home, one loss away, against the SAME opponent. If the perspective flip is
    wrong this comes back 2-0 or 0-2 instead of 1-1."""
    panel = await basketball_panel("5", "9", "nba")
    assert panel is not None
    assert (panel.meetings_count, panel.home_wins, panel.away_wins) == (2, 1, 1)
    assert panel.draws == 0, "basketball cannot draw"


async def test_basketball_points_are_averaged_per_side_not_per_venue(fake_games):
    """Our home team scored 110 then 90 -> 100.0; the opponent 100 then 95 -> 97.5. Averaging
    by the meeting's own home/away slot instead would mix the two teams together."""
    panel = await basketball_panel("5", "9", "nba")
    points = next(s for s in panel.stats if s.label == "Points")
    assert (points.home, points.away) == (100.0, 97.5)


async def test_games_against_other_opponents_are_excluded(fake_games):
    """The 120-80 win over team 77 sits in the same fetched history and would badly distort
    the averages if the opponent filter were wrong."""
    panel = await basketball_panel("5", "9", "nba")
    assert panel.meetings_count == 2


async def test_two_teams_that_have_never_met_return_none(fake_games):
    """None, never a fabricated 0-0 record with empty stats."""
    assert await basketball_panel("5", "404", "nba") is None


async def test_basketball_offers_exactly_the_two_rows_the_provider_supports(fake_games):
    """A guard on the honest limit: if someone adds a rebounds row here it will be fabricated,
    because /stats is 401 and no box score is reachable."""
    panel = await basketball_panel("5", "9", "nba")
    # ONE row: "points allowed" is the exact mirror of "points scored" in a two-team H2H, so a
    # second row would fill the panel without adding a fact.
    assert [s.label for s in panel.stats] == ["Points"]
    assert all(s.suffix == "" for s in panel.stats)
