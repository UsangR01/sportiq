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
