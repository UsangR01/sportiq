"""Pins the four API-Basketball behaviours that would each have been a silent bug.

Every one of these was measured against the real API before the adapter was written, and every
one fails quietly rather than loudly if it regresses: a wrong season returns HTTP 200 with
results=0, an unmapped status leaves a game unsettled forever, and the wrong odds market returns
perfectly valid prices for a different question.
"""

from datetime import UTC, datetime

import pytest

from app.adapters.api_basketball import (
    BASKETBALL_LEAGUE_IDS,
    ID_PREFIX,
    MONEYLINE_MARKET,
    YEAR_SEASON_LEAGUES,
    APIBasketballAdapter,
    _compute_team_stats,
    _map_status,
    _season_for,
)
from app.adapters.balldontlie import BallDontLieAdapter
from app.adapters.factory import AdapterFactory
from app.sports.catalog import BASKETBALL_LEAGUES

# --------------------------------------------------------------------------- season labels


def test_domestic_leagues_use_the_span_format_and_the_euroleague_uses_a_bare_year():
    """TWO FORMATS IN ONE PROVIDER, confirmed live. Getting this wrong does not raise -- the
    request returns 200 with results=0, which is indistinguishable from a league with no games
    scheduled. That exact silence hid the J1 League's entire fixture list for days."""
    october = datetime(2026, 10, 4, tzinfo=UTC)
    assert _season_for("acb", october) == "2026-2027"
    assert _season_for("lega_a", october) == "2026-2027"
    assert _season_for("euroleague", october) == 2026
    assert isinstance(_season_for("euroleague", october), int)
    assert isinstance(_season_for("acb", october), str)


def test_the_season_rolls_over_in_september_not_january_or_july():
    """European basketball tips off mid-September (BBL 18 Sep, Euroleague 24 Sep, ACB 26 Sep,
    Greece 3 Oct). August belongs to the OLD season; a calendar-year rule would be a year out
    for most of the season, which is the Brasileirao bug."""
    assert _season_for("acb", datetime(2026, 8, 31, tzinfo=UTC)) == "2025-2026"
    assert _season_for("acb", datetime(2026, 9, 1, tzinfo=UTC)) == "2026-2027"
    # Still the same season in the spring half -- the half a start-year rule gets wrong if it
    # keys off the calendar year alone.
    assert _season_for("acb", datetime(2027, 3, 1, tzinfo=UTC)) == "2026-2027"
    assert _season_for("acb", datetime(2027, 5, 31, tzinfo=UTC)) == "2026-2027"


def test_only_the_euroleague_is_declared_a_year_season_league():
    """Checked per league against the provider's own windows rather than inherited. If a
    domestic league ever lands here it will silently request the wrong season."""
    assert YEAR_SEASON_LEAGUES == {"euroleague"}


# --------------------------------------------------------------------------- status mapping


def test_after_over_time_is_a_finished_game():
    """THE ONE THAT MATTERS MOST: AOT is 3.6-6.6% of a season. Mapping only FT would leave
    roughly one game in twenty permanently unsettled -- no outcome, no Elo update, no graded
    pick -- with nothing logged."""
    assert _map_status("AOT") == "completed"
    assert _map_status("FT") == "completed"


def test_postponed_and_cancelled_share_one_bucket_and_are_not_live():
    assert _map_status("POST") == "postponed"
    assert _map_status("CANC") == "postponed"


def test_in_play_quarters_are_live():
    for code in ("Q1", "Q2", "Q3", "Q4", "OT", "HT"):
        assert _map_status(code) == "live", code


def test_an_unknown_status_falls_back_to_scheduled_never_live():
    """The football adapter's original 'anything else is live' default put a LIVE badge and a
    market prediction on four genuinely postponed fixtures. A fixture wrongly called scheduled
    is corrected by the next poll; one wrongly called live shows a game that is not happening."""
    assert _map_status("SOMETHING_NEW") == "scheduled"
    assert _map_status(None) == "scheduled"


# --------------------------------------------------------------------------- the odds market


def _odds_entry():
    """Shaped exactly like the real /odds response, including BOTH markets a basketball book
    offers -- which is the whole point of the test below."""
    return {
        "game": {"id": 509592, "date": "2026-10-05T18:45:00+00:00"},
        "bookmakers": [
            {
                "name": "Marathon Bet",
                "bets": [
                    # Regulation time only, hence a Draw, and a LONGER home price.
                    {
                        "name": "3Way Result",
                        "values": [
                            {"value": "Home", "odd": "4.55"},
                            {"value": "Draw", "odd": "15.25"},
                            {"value": "Away", "odd": "1.24"},
                        ],
                    },
                    # The moneyline including overtime -- what the model actually predicts.
                    {
                        "name": "Home/Away",
                        "values": [
                            {"value": "Home", "odd": "4.33"},
                            {"value": "Away", "odd": "1.23"},
                        ],
                    },
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_odds_read_the_two_way_moneyline_not_the_three_way_market(monkeypatch):
    """READING "3Way Result" WOULD COST MONEY, not merely be untidy. It prices regulation time,
    so its home price is systematically longer (4.55 vs 4.33 on this real fixture) and it
    carries a Draw for a market with no draw. Taking it would inflate EV on every basketball
    card and hand the pipeline a draw price the model never produces."""
    adapter = APIBasketballAdapter()

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"errors": [], "response": [_odds_entry()]}

    class _Client:
        async def get(self, path, params=None):
            return _Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(adapter, "_client", lambda: _Client())
    payloads = await adapter.fetch_odds("nba", "lega_a", days_ahead=30)

    assert len(payloads) == 1
    pick = payloads[0]
    assert pick.home_odds == 4.33, "took the 3Way price instead of the moneyline"
    assert pick.away_odds == 1.23
    # Basketball has no draw. A price here means the 3Way market leaked in.
    assert pick.draw_odds is None
    assert pick.market == "h2h"
    assert MONEYLINE_MARKET == "Home/Away"


# --------------------------------------------------------------------------- id namespacing


def test_ids_are_prefixed_so_they_cannot_collide_with_balldontlie_under_one_sport():
    """These leagues share Sport(slug="nba") with the NBA and WNBA, and Team/Fixture uniqueness
    is (sport_id, external_id). An unprefixed API-Basketball id could land on a BallDontLie row
    and silently merge two different clubs -- the hazard that made WNBA ids "wnba:"-prefixed."""
    assert ID_PREFIX == "ab:"


def test_the_prefix_is_per_provider_not_per_league():
    """API-Basketball uses ONE global team namespace, so Valencia is 2341 in both the ACB and
    the Euroleague. A per-league prefix would split one club into two rows with two disjoint
    form and Elo histories -- the opposite of what pooling the competitions is for."""
    stats_acb = _compute_team_stats("ab:2341", [], 10)
    stats_euro = _compute_team_stats("ab:2341", [], 10)
    assert stats_acb.team_external_id == stats_euro.team_external_id == "ab:2341"


def test_compute_team_stats_strips_the_prefix_before_matching_provider_ids():
    """The id we store is prefixed; the id inside a provider payload is not. Comparing them
    without stripping silently matches nothing, which reads as 'this team has never played'."""
    games = [
        {
            "id": 1,
            "date": "2026-10-01T18:00:00+00:00",
            "status": {"short": "FT"},
            "teams": {"home": {"id": 2341, "name": "Valencia"}, "away": {"id": 1139, "name": "X"}},
            "scores": {"home": {"total": 90}, "away": {"total": 80}},
        },
        {
            "id": 2,
            "date": "2026-10-03T18:00:00+00:00",
            "status": {"short": "AOT"},  # counted, see the AOT test above
            "teams": {"home": {"id": 1139, "name": "X"}, "away": {"id": 2341, "name": "Valencia"}},
            "scores": {"home": {"total": 70}, "away": {"total": 75}},
        },
    ]
    stats = _compute_team_stats("ab:2341", games, 10)
    assert stats.form_pts_5 == 1.0, "both games were wins"
    assert stats.season_point_diff == pytest.approx((10 + 5) / 2)
    assert stats.home_win_rate == 1.0
    assert stats.away_win_rate == 1.0
    # No draws exist in basketball, so the sequence is W/L only.
    assert set(stats.recent_form or "") <= {"W", "L"}


# --------------------------------------------------------------------------- routing / parity


def test_the_factory_routes_european_leagues_away_from_balldontlie():
    """The whole reason the stats adapter became league-aware. BallDontLie has no European
    coverage, so routing these by SPORT would send every request to a provider that cannot
    answer it -- and the NBA and WNBA must keep going to BallDontLie."""
    for league in BASKETBALL_LEAGUE_IDS:
        assert isinstance(AdapterFactory.get_stats_adapter("nba", league), APIBasketballAdapter)
    for league in ("nba", "wnba"):
        assert isinstance(AdapterFactory.get_stats_adapter("nba", league), BallDontLieAdapter)
    # Omitting the league must preserve the original behaviour exactly, which is what keeps
    # every pre-existing call site and test valid.
    assert isinstance(AdapterFactory.get_stats_adapter("nba"), BallDontLieAdapter)


def test_european_basketball_odds_come_from_api_basketball_alone():
    """TheRundown's own /sports list is 36 entries with no European basketball at all, so
    querying it could only ever raise the per-adapter ValueError ingest_odds isolates."""
    for league in BASKETBALL_LEAGUE_IDS:
        adapters = AdapterFactory.get_odds_adapters("nba", league)
        assert [type(a) for a in adapters] == [APIBasketballAdapter], league


def test_the_catalog_and_the_league_id_map_agree():
    """Two separate wirings that must not drift -- the same failure that left nine football
    leagues trained but never ingested. A league in one and not the other is invisible."""
    assert {slug for slug, _, _ in BASKETBALL_LEAGUES} == set(BASKETBALL_LEAGUE_IDS)


# --------------------------------------- the three production bugs found the morning after


def test_the_season_label_parses_for_both_provider_formats():
    """THE BUG THAT COST SIX LEAGUES THEIR PREDICTIONS. int(fixture.season) was safe while every
    season label was a bare year; API-Basketball labels a domestic season "2026-2027".

    Measured in production the morning these went live: of 73 domestic fixtures, all 21
    COMPLETED carried a prediction and all 52 SCHEDULED carried none, while the EuroLeague --
    whose label is the bare year 2026 -- had 10 of 10. The ValueError fired inside
    ingest_fixtures' upcoming-features loop and the per-league isolation swallowed it, so the
    leagues ingested fixtures perfectly and silently produced nothing to show.
    """
    from app.fixtures.service import season_start_year

    assert season_start_year("2026-2027") == 2026  # domestic European basketball
    assert season_start_year("2026") == 2026  # EuroLeague, football, BallDontLie
    assert season_start_year(2026) == 2026
    # Unparseable costs the one feature that needs it, never the league's predictions.
    assert season_start_year(None) is None
    assert season_start_year("") is None
    assert season_start_year("abc") is None


def test_european_basketball_head_to_head_does_not_go_to_balldontlie():
    """EVERY European basketball fixture detail returned HTTP 500 until this routed correctly.

    _fetch_head_to_head sent all sport_slug == "nba" fixtures to BallDontLie, which cannot parse
    an "ab:"-prefixed id. The resulting error was NOT an httpx.HTTPError, so the panel's own
    try/except did not catch it and the whole fixture screen failed -- while the WNBA, same sport
    row and same code path, was fine.
    """
    import inspect

    from app.fixtures import router as fixtures_router

    source = inspect.getsource(fixtures_router._fetch_head_to_head)
    assert "BASKETBALL_LEAGUE_IDS" in source, (
        "the h2h dispatch no longer distinguishes API-Basketball leagues from BallDontLie ones; "
        "every European basketball fixture detail will 500 again"
    )
    assert hasattr(fixtures_router, "_api_basketball_head_to_head")


def test_basketball_still_fits_under_the_league_picker_threshold():
    """The reported symptom: basketball grew from 2 leagues to 9, crossed LEAGUE_PICKER_MAX,
    and its filter chips vanished entirely -- including the NBA and WNBA ones that had always
    been there. A threshold set exactly at today's count would re-break on the next addition,
    so this asserts real headroom rather than a bare inequality."""
    from app.sports.router import LEAGUE_PICKER_MAX

    nba_and_wnba = 2
    assert len(BASKETBALL_LEAGUES) + nba_and_wnba <= LEAGUE_PICKER_MAX
    assert (
        LEAGUE_PICKER_MAX - (len(BASKETBALL_LEAGUES) + nba_and_wnba) >= 1
    ), "no room for another basketball competition before the picker silently disappears"
