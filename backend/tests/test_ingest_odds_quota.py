"""Guards the odds-request budget.

These exist because of a real, weeks-long outage rather than a hypothetical: TheRundown's
odds endpoint is one request per DATE, and ingest_odds used to walk every day in a 7-day
lookahead for every league, every 5 minutes — roughly 16,000 requests/day against a
1,000-request MONTHLY quota. It exhausted the allowance in about 90 minutes and then 429'd
for the rest of the month, leaving only 6 of 51 upcoming football fixtures with any odds.

The damage surfaced somewhere unexpected: with no odds, expected-value ranking and the
min_odds filter both silently degrade to probability-only behaviour, so it looked like a
modelling problem (a feed full of near-identical high-probability picks) rather than an
ingestion one. Hence tests on the request budget itself, not just on parsing.
"""

from datetime import date, timedelta

import pytest

from app.adapters.therundown import TheRundownAdapter


class _RecordingClient:
    """Stands in for the httpx client so we can count requests without network access."""

    def __init__(self):
        self.paths: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, path, params=None):
        self.paths.append(path)

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"events": []}

            @staticmethod
            def raise_for_status():
                return None

        return _Response()


async def test_fetch_odds_requests_only_the_dates_it_was_given(monkeypatch):
    """The core protection: passing explicit dates must cost one request per date, NOT one
    per day of the lookahead window."""
    adapter = TheRundownAdapter()
    recorder = _RecordingClient()
    monkeypatch.setattr(adapter, "_client", lambda: recorder)
    monkeypatch.setattr("app.adapters.therundown.ODDS_REQUEST_DELAY_SECONDS", 0)

    wanted = [date(2026, 8, 5), date(2026, 8, 9)]
    await adapter.fetch_odds(sport="football", league="epl", days_ahead=7, dates=wanted)

    assert len(recorder.paths) == 2, "should cost one request per supplied date"
    assert recorder.paths[0].endswith("/2026-08-05")
    assert recorder.paths[1].endswith("/2026-08-09")


async def test_fetch_odds_falls_back_to_the_full_window_without_dates(monkeypatch):
    """Backwards compatibility: an adapter called without dates keeps the original
    every-day-in-the-window behaviour, so nothing silently stops fetching."""
    adapter = TheRundownAdapter()
    recorder = _RecordingClient()
    monkeypatch.setattr(adapter, "_client", lambda: recorder)
    monkeypatch.setattr("app.adapters.therundown.ODDS_REQUEST_DELAY_SECONDS", 0)

    await adapter.fetch_odds(sport="football", league="epl", days_ahead=3)

    assert len(recorder.paths) == 4, "days_ahead=3 covers today plus the next three days"


async def test_dates_with_fixtures_ignores_leagues_with_nothing_scheduled():
    """A league between seasons must cost ZERO odds requests. Previously it still cost one
    per lookahead day, which is where most of the wasted quota went - the majority of
    league-days in the window have no fixtures at all."""
    from app.workers.ingest_odds import _dates_with_fixtures

    class _Sport:
        slug = "football"

    class _League:
        id = "00000000-0000-0000-0000-000000000000"
        slug = "no-such-league"

    assert await _dates_with_fixtures(_Sport(), _League()) == []


def test_lookahead_and_cadence_stay_within_a_sane_request_budget():
    """A tripwire on the two constants that caused the outage. If either is widened again,
    this fails and forces a deliberate decision about the quota rather than a silent
    regression back to ~16,000 requests/day."""
    from app.workers.celery import celery_app
    from app.workers.ingest_odds import ODDS_LOOKAHEAD_DAYS

    schedule = celery_app.conf.beat_schedule["ingest-odds-every-6-hours"]["schedule"]
    runs_per_day = 86400 / float(schedule)
    rundown_leagues = 7  # see app/adapters/therundown.py:_RUNDOWN_SPORT_IDS

    # Worst case: every league has a fixture on every day of the lookahead window.
    worst_case_per_day = rundown_leagues * (ODDS_LOOKAHEAD_DAYS + 1) * runs_per_day
    assert worst_case_per_day <= 200, (
        f"worst-case {worst_case_per_day:.0f} odds requests/day is too close to the quota; "
        "TheRundown's BASIC plan allows 1,000 per MONTH"
    )
    assert timedelta(seconds=float(schedule)) >= timedelta(hours=1)


def test_tennis_refresh_is_hourly():
    """The tennis job's exemption from the 6-hourly cadence is only valid while it stays on a
    provider with no monthly cap. The cadence itself is asserted here; the provider list is
    asserted behaviourally in the test below."""
    from app.workers.celery import celery_app

    assert float(celery_app.conf.beat_schedule["ingest-tennis-odds-hourly"]["schedule"]) == 3600.0


@pytest.mark.asyncio
async def test_tennis_refresh_never_reaches_therundown():
    """Asserts the ADAPTERS ACTUALLY PASSED, not the source text.

    If TheRundown were ever added here, the hourly cadence would silently become
    7 leagues x 4 days x 24 runs/day -- far past the 5,000/month allowance, reproducing the
    original outage. So this inspects what _ingest_odds_for_league is really handed.
    """
    from unittest.mock import patch

    from app.adapters.balldontlie_tennis import BallDontLieTennisAdapter
    from app.adapters.therundown import TheRundownAdapter
    from app.workers import ingest_odds as module

    seen = []

    async def capture(sport, league, adapters=None):
        seen.append(adapters)

    with (
        patch.object(module, "_ingest_odds_for_league", side_effect=capture),
        patch.object(module, "async_session_factory") as factory,
    ):
        factory.return_value.__aenter__.return_value = _tennis_session()
        await module._ingest_tennis_odds()

    assert seen, "no league was processed"
    for adapters in seen:
        types = [type(a) for a in adapters]
        assert types == [BallDontLieTennisAdapter], types
        assert TheRundownAdapter not in types


@pytest.mark.asyncio
async def test_tennis_refresh_survives_one_tour_failing():
    """WTA still 401s on every GOAT endpoint (the subscription is ATP-only), and that must not
    stop ATP's prices from landing - the same per-league isolation ingest_fixtures applies."""
    from unittest.mock import patch

    from app.workers import ingest_odds as module

    calls = []

    async def flaky(sport, league, adapters=None):
        calls.append(league.slug)
        if league.slug == "wta":
            raise RuntimeError("401 Unauthorized")

    with (
        patch.object(module, "_ingest_odds_for_league", side_effect=flaky),
        patch.object(module, "async_session_factory") as factory,
    ):
        factory.return_value.__aenter__.return_value = _tennis_session()
        await module._ingest_tennis_odds()

    assert "atp" in calls and "wta" in calls, calls


def _tennis_session():
    """Minimal async session stub returning one tennis Sport and two League rows."""
    from unittest.mock import AsyncMock

    class _Sport:
        id = "s"
        slug = "tennis"
        active = True

    class _League:
        def __init__(self, slug):
            self.slug = slug
            self.id = slug
            self.sport_id = "s"
            self.active = True

    session = AsyncMock()

    def execute_result(*_a, **_k):
        result = AsyncMock()
        result.scalar_one_or_none = lambda: _Sport()
        scalars = AsyncMock()
        scalars.all = lambda: [_League("atp"), _League("wta")]
        result.scalars = lambda: scalars
        return result

    session.execute = AsyncMock(side_effect=execute_result)
    return session
