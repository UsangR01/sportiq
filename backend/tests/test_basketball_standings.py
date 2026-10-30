"""Basketball gets a league table, and it is NOT football-shaped.

Three measured provider behaviours sit behind this, each of which fails silently rather than
loudly if it is got wrong -- which is why they are pinned here rather than trusted to review.
"""

import pytest

from app.adapters.api_basketball import (
    BASKETBALL_LEAGUE_IDS,
    STANDINGS_LEAGUE_IDS,
    YEAR_SEASON_LEAGUES,
    _season_for,
)
from app.fixtures.router import (
    _STANDINGS_COLUMNS,
    _STANDINGS_SPORTS,
    _normalise_club_name,
    _standings_values,
)


def test_the_standings_map_does_not_change_adapter_routing():
    """THE DANGEROUS ONE. STANDINGS_LEAGUE_IDS adds the NBA and WNBA because BallDontLie
    cannot serve a table at any tier we hold (/standings is 401 for both, measured). But
    BASKETBALL_LEAGUE_IDS drives ADAPTER ROUTING -- factory.py, the H2H dispatch and
    nba_features all key off it -- so adding them there would send NBA fixtures, odds and team
    stats to API-Basketball instead of BallDontLie, which owns them.
    """
    assert "nba" in STANDINGS_LEAGUE_IDS and "wnba" in STANDINGS_LEAGUE_IDS
    assert "nba" not in BASKETBALL_LEAGUE_IDS
    assert "wnba" not in BASKETBALL_LEAGUE_IDS
    # Every routed league must still be fetchable, or its table silently disappears.
    assert set(BASKETBALL_LEAGUE_IDS) <= set(STANDINGS_LEAGUE_IDS)


def test_the_wnba_season_is_a_calendar_year_not_a_span():
    """A wrong season here does NOT raise -- it returns HTTP 200 with results=0, which reads
    exactly like a league with nothing scheduled. Measured live: "2026-2027" returned 0 rows
    and "2026" returned 30."""
    assert "wnba" in YEAR_SEASON_LEAGUES
    assert _season_for("wnba") == 2026 or isinstance(_season_for("wnba"), int)
    # A domestic European league must still get the span form.
    assert isinstance(_season_for("acb"), str) and "-" in str(_season_for("acb"))


def test_basketball_does_not_display_points_because_the_provider_reports_zero():
    """The NBA and WNBA rank on WIN-LOSS RECORD, and API-Basketball returns group.points = 0
    for them (measured). Reusing football's PTS column would print a column of zeros, so the
    last column is WINS and the server says so."""
    assert _STANDINGS_COLUMNS["nba"] == ["P", "DIFF", "W"]
    assert _STANDINGS_COLUMNS["football"] == ["P", "GD", "PTS"]

    row = {"played": 44, "goal_difference": 316, "points": 0, "won": 33}
    assert _standings_values("nba", row) == [44, 316, 33], "basketball must show wins, not points"
    assert _standings_values("football", row) == [44, 316, 0]


def test_tennis_has_no_table_by_nature_rather_than_by_gap():
    """Tennis is absent deliberately: there is no league table in tennis. Its analogue is the
    tour ranking, which is a different panel, so the endpoint returns None rather than an
    empty table that looks like a loading failure."""
    assert _STANDINGS_SPORTS == {"football", "nba"}
    assert "tennis" not in _STANDINGS_SPORTS


@pytest.mark.parametrize(
    ("provider_name", "ours"),
    [
        ("Minnesota Lynx W", "Minnesota Lynx"),
        ("Golden State Valkyries W", "Golden State Valkyries"),
        ("Houston Rockets", "Houston Rockets"),
    ],
)
def test_wnba_clubs_match_ours_despite_the_providers_trailing_w(provider_name, ours):
    """The NBA and WNBA tables come from a DIFFERENT provider than their fixtures, so their
    ids can never match and the name is the only link. API-Basketball suffixes every WNBA club
    with " W"; without stripping it, not one WNBA row would link to its team."""
    assert _normalise_club_name(provider_name) == _normalise_club_name(ours)


# ------------------------------------------------------------------ richer H2H via the
# ------------------------------------------------------------------ other provider


def test_the_cross_provider_name_key_strips_the_wnba_suffix():
    """Same join as the standings table, and it carries more weight here: the H2H panel feeds
    box scores, so a wrong match returns a real, confident, WRONG head-to-head rather than an
    obviously empty one."""
    from app.adapters.api_basketball import normalise_team_name

    assert normalise_team_name("Minnesota Lynx W") == normalise_team_name("Minnesota Lynx")
    assert normalise_team_name("  Boston Celtics ") == normalise_team_name("boston celtics")
    # Not over-eager: a club whose name merely CONTAINS a W is untouched.
    assert normalise_team_name("Washington Wizards") == "washington wizards"


@pytest.mark.asyncio
async def test_an_unresolvable_club_falls_back_rather_than_guessing(monkeypatch):
    """The failure that matters. If either club cannot be resolved in the other provider's
    namespace the enriched panel must return None so the caller degrades to the thin-but-
    correct BallDontLie panel -- never a partial or guessed pairing.
    """
    import app.adapters.api_basketball as ab
    from app.fixtures.models import Team
    from app.fixtures.router import _basketball_head_to_head_by_name

    async def only_one(league, names):
        return {names[0]: 123}  # the away club is unresolvable

    monkeypatch.setattr(ab, "resolve_team_ids_by_name", only_one)

    result = await _basketball_head_to_head_by_name(
        "nba", Team(name="Boston Celtics"), Team(name="Mystery Club")
    )
    assert result is None, "a half-resolved pairing must not produce a panel"
