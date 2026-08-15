"""H2H fed the model evidence from competitions the model never trained on.

/fixtures/headtohead returns every competition two clubs have ever met in, at any age. The
TRAINING counterpart, football_features._h2h_stats, reads the collected game log -- which
ml/training/collect_football_data.py gathers PER LEAGUE for SEASONS (2021-2025). So three
features (h2h_win_rate_home, h2h_avg_goals_scored_home, h2h_avg_goals_allowed_home) were
learned from same-competition meetings and served from a mixture that included friendlies,
cup ties and second-tier matches.

Measured over 153 real upcoming fixtures before the fix: 106 (69%) had at least one of the
three move, mean |delta win_rate| 0.103, max 0.800. The off-competition meetings being counted
were 47 friendlies, 25 Championship, 21 FA Cup, 20 J2 League, 18 League One, 11 League Cup.

The case that motivated it: Mito Hollyhock v Gamba Osaka (J1 League). All four counted
meetings were a J-League Cup tie, a friendly, and two 2013 J2 matches -- not one J1 match.
"""

import ast
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.adapters import api_football
from app.adapters.api_football import (
    H2H_LOOKBACK_MEETINGS,
    H2H_TRAINING_WINDOW_START,
    _fetch_h2h_meetings,
    fetch_h2h_stats,
)

EPL = 39
FA_CUP = 45
FRIENDLIES = 667

# Home side is 33 throughout. Two same-league meetings inside the training window (one won,
# one lost), plus three that must not count: a cup tie, a friendly, and a league meeting from
# before the collected history begins.
MEETINGS = [
    ("2025-04-16T15:00:00+00:00", EPL, 2, 0),  # in scope, home won
    ("2024-11-02T15:00:00+00:00", EPL, 0, 1),  # in scope, home lost
    ("2025-01-10T15:00:00+00:00", FA_CUP, 3, 0),  # cup tie
    ("2025-07-20T15:00:00+00:00", FRIENDLIES, 4, 0),  # pre-season friendly
    ("2013-09-15T15:00:00+00:00", EPL, 5, 0),  # right league, before the training window
]


def _payload():
    return {
        "response": [
            {
                "fixture": {
                    "id": index,
                    "date": meeting_date,
                    "status": {"long": "Match Finished", "short": "FT", "elapsed": 90},
                },
                "league": {"id": league_id, "name": f"league-{league_id}"},
                "teams": {"home": {"id": 33, "name": "Home"}, "away": {"id": 42, "name": "Away"}},
                "goals": {"home": home_goals, "away": away_goals},
            }
            for index, (meeting_date, league_id, home_goals, away_goals) in enumerate(MEETINGS)
        ]
    }


@pytest.fixture
def mocked_provider(monkeypatch):
    """_fetch_h2h_meetings builds its own AsyncClient, so the transport is injected by wrapping
    the class rather than by passing one in. Records the query params so the request itself can
    be asserted on, not just the parsed result."""
    seen: dict = {}
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=_payload())

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(api_football.httpx, "AsyncClient", factory)
    return seen


@pytest.mark.asyncio
async def test_unfiltered_by_default_so_the_display_panel_is_unchanged(mocked_provider):
    """fetch_h2h_detail passes neither filter. Narrowing the panel is a separate product
    decision with the OPPOSITE pull -- it would empty a card rather than enrich it -- so the
    default must stay exactly as it was."""
    meetings = await _fetch_h2h_meetings("33", "42")

    assert len(meetings) == len(MEETINGS)


@pytest.mark.asyncio
async def test_filters_to_the_fixtures_own_competition(mocked_provider):
    meetings = await _fetch_h2h_meetings("33", "42", league_external_id=EPL)

    assert {fx["league"]["id"] for fx in meetings} == {EPL}
    assert len(meetings) == 3  # the 2013 one still counts without a `since`


@pytest.mark.asyncio
async def test_excludes_meetings_older_than_the_collected_history(mocked_provider):
    """Serving reached back to 2013 for a real fixture. Training cannot see anything before
    SEASONS[0], so such a meeting is evidence the model was never shown."""
    meetings = await _fetch_h2h_meetings(
        "33", "42", league_external_id=EPL, since=H2H_TRAINING_WINDOW_START
    )

    assert [fx["fixture"]["id"] for fx in meetings] == [0, 1]


@pytest.mark.asyncio
async def test_the_model_path_applies_both_filters(mocked_provider):
    """Unfiltered, the home side looks far stronger than it is: the cup rout, the friendly and
    the 2013 win are all counted. Filtered, it is one win from two."""
    unfiltered = await fetch_h2h_stats("33", "42")
    filtered = await fetch_h2h_stats("33", "42", EPL)

    assert unfiltered.win_rate_home == pytest.approx(4 / 5)
    assert unfiltered.avg_goals_scored_home == pytest.approx(14 / 5)

    assert filtered.win_rate_home == pytest.approx(1 / 2)
    assert filtered.avg_goals_scored_home == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_a_promoted_club_gets_no_h2h_rather_than_a_second_tier_number(monkeypatch):
    """THE CASE THE FIX EXISTS FOR. All 13 fixtures that lost H2H entirely in the measurement
    were promoted clubs -- Coventry, Hull City, Ipswich, Racing Santander, Deportivo, Malaga,
    Wieczysta Krakow. A promoted club has no top-flight history, so _h2h_stats yields None
    during training. Serving was handing the model a Championship-derived number instead.

    None is the honest answer, and XGBoost handles the resulting NaN natively."""
    real_client = httpx.AsyncClient
    championship_only = {
        "response": [
            {
                "fixture": {
                    "id": 1,
                    "date": "2025-03-01T15:00:00+00:00",
                    "status": {"long": "Match Finished", "short": "FT", "elapsed": 90},
                },
                "league": {"id": 40, "name": "Championship"},
                "teams": {"home": {"id": 33, "name": "Home"}, "away": {"id": 42, "name": "Away"}},
                "goals": {"home": 2, "away": 0},
            }
        ]
    }

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, json=championship_only)
        )
        return real_client(*args, **kwargs)

    monkeypatch.setattr(api_football.httpx, "AsyncClient", factory)

    assert await fetch_h2h_stats("33", "42") is not None  # today's behaviour, for contrast
    assert await fetch_h2h_stats("33", "42", EPL) is None


@pytest.mark.asyncio
async def test_a_league_we_cannot_resolve_degrades_to_the_old_behaviour(mocked_provider):
    """A fixture whose slug is not in LEAGUE_IDS passes None, and must keep its features rather
    than silently losing three of them."""
    assert await fetch_h2h_stats("33", "42", None) is not None


@pytest.mark.asyncio
async def test_a_wider_page_is_requested_because_the_provider_filters_after_paging(
    mocked_provider,
):
    """`last` is applied by the provider BEFORE our competition filter, so a 10-meeting page
    shrinks once filtered. Measured over 45 real fixtures: last=10 leaves a mean of 5.42
    qualifying meetings and last=40 leaves 5.98, recovering ZERO fixtures from empty -- so 10
    was nearly enough, and 20 captures the remainder at the same single API call."""
    await fetch_h2h_stats("33", "42", EPL)

    assert mocked_provider["params"]["last"] == str(H2H_LOOKBACK_MEETINGS)
    assert H2H_LOOKBACK_MEETINGS == 20


def test_the_window_start_tracks_the_collected_history():
    """PARITY GUARD, in the spirit of test_form_window_parity/test_train_serve_league_parity.
    If collection ever starts from a different season, this bound is silently wrong and the
    serving features quietly stop matching what training saw.

    Parsed with ast rather than imported: collect_football_data.py pulls in pandas and httpx at
    module scope and nothing in it is reachable from CI."""
    source = (
        Path(__file__).resolve().parents[2] / "ml" / "training" / "collect_football_data.py"
    ).read_text(encoding="utf-8")
    seasons = next(
        ast.literal_eval(node.value)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "SEASONS" for t in node.targets)
    )

    assert H2H_TRAINING_WINDOW_START == date(min(seasons), 7, 1)
