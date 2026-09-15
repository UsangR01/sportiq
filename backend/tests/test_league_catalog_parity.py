"""Everything we ingest must be seedable, and everything seeded must be ingestable.

THIS DRIFT HAS ALREADY COST REAL WORK. The nine Tier-1 leagues were collected, trained into the
pooled model and seeded while api_football.py's LEAGUE_IDS still listed nine -- so the model had
learned from leagues the app fetched nothing for, and no fixture, odds or prediction ever reached
a user. Nothing errored. The work simply never arrived, which is this project's most expensive
recurring failure shape.

The two lists are separate wirings by necessity (one is a provider id map, the other is display
data), so the guard is a test rather than a merge.
"""

from app.adapters.api_football import (
    CALENDAR_YEAR_SEASON_LEAGUES,
    END_YEAR_SEASON_LEAGUES,
    LEAGUE_IDS,
)
from app.sports.catalog import FOOTBALL_LEAGUES

CATALOG_SLUGS = {slug for slug, _name, _country in FOOTBALL_LEAGUES}


def test_every_ingestable_league_can_be_seeded():
    """A LEAGUE_IDS entry with no catalog row is fetched for nothing -- ingest iterates League
    ROWS, so the fixtures have nowhere to land."""
    assert sorted(set(LEAGUE_IDS) - CATALOG_SLUGS) == []


def test_every_seeded_league_is_ingestable():
    """The direction that actually bit: a seeded league with no provider id is a permanently
    empty section of the app."""
    assert sorted(CATALOG_SLUGS - set(LEAGUE_IDS)) == []


def test_league_slugs_are_unique():
    slugs = [slug for slug, _n, _c in FOOTBALL_LEAGUES]
    assert len(slugs) == len(set(slugs))


def test_the_six_added_in_september_are_wired_end_to_end():
    """Pinned by name because they were chosen on measured coverage -- 8-9 bookmakers pricing
    four markets, 11-13 seasons of statistics, real xG -- and a silent drop would undo that
    without any failure."""
    for slug in (
        "eredivisie",
        "primeira_liga",
        "belgian_pro",
        "super_lig",
        "bundesliga_2",
        "serie_b",
    ):
        assert slug in CATALOG_SLUGS, f"{slug} lost its catalog row"
        assert slug in LEAGUE_IDS, f"{slug} lost its provider id"


def test_the_new_leagues_use_the_ordinary_european_season_convention():
    """Confirmed live per league, not inherited. API-Football labels a 2026-08 -> 2027-05 window
    as season 2026 for all six, so they follow the START-year rule.

    Getting this wrong is silent and total: the Brasileirão bug computed a season a full year off
    for most of the year, and the J1 League needed a THIRD convention nobody could have derived
    from the other two.
    """
    for slug in (
        "eredivisie",
        "primeira_liga",
        "belgian_pro",
        "super_lig",
        "bundesliga_2",
        "serie_b",
    ):
        assert slug not in CALENDAR_YEAR_SEASON_LEAGUES
        assert slug not in END_YEAR_SEASON_LEAGUES
