"""TheStatsAPI as a SECOND corner source, and why it is not a live one.

Corner counts drive the tick or cross on a settled corners pick; without one the card shows a
neutral grey badge on a finished match. Measured 2026-08-14, API-Football's live coverage is
165/190 (86.8%) and the misses are concentrated: veikkausliiga 0/7, brasileirao 17/32.
TheStatsAPI -- already provisioned, already paid for, already used for xG -- carries corners for
99.8% of both.

LATENCY, NOT COVERAGE, IS THE CONSTRAINT. Measured against the real API: a match 3.4 hours past
kickoff was already `finished` and its /stats returned HTTP 404, while every sampled match five
or more days old carried real corners. So the cadence is a QUOTA decision -- asking every five
minutes would spend hundreds of metered calls per fixture learning "not yet".
"""

from datetime import date

import pytest

from app.adapters import thestatsapi


def test_the_gap_leagues_are_actually_mapped():
    """The whole point. Veikkausliiga is 0/7 on API-Football, so an unmapped competition id
    here would leave the one league that most needs a second source with no second source."""
    for league in ("veikkausliiga", "brasileirao", "j1_league"):
        assert league in thestatsapi.COMPETITION_IDS


def test_every_trained_league_is_mapped():
    """One model serves all 22, so any of them can surface a corners pick."""
    assert len(thestatsapi.COMPETITION_IDS) == 22
    assert all(v.startswith("comp_") for v in thestatsapi.COMPETITION_IDS.values())


async def test_an_unmapped_league_is_declined_without_a_call(monkeypatch):
    """No key is read and no request made -- so a sport or league this provider does not carry
    cannot spend quota discovering that."""
    called = False

    def _boom():
        nonlocal called
        called = True
        raise AssertionError("must not build a client for an unmapped league")

    monkeypatch.setattr(thestatsapi, "_client", _boom)
    assert await thestatsapi.fetch_corners("nba", date(2026, 8, 14), 1, 0) is None
    assert called is False


def test_missing_key_is_a_named_deployment_error():
    """Distinct from an HTTP failure: one is a gap the operator can close, the other is the
    provider having a bad day. This key lived only in keys.docx for the offline collector, so a
    deployed worker genuinely would not have had it."""
    assert issubclass(thestatsapi.TheStatsAPINotConfigured, RuntimeError)


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"data": {"overview": {"corner_kicks": {"all": {"home": 3, "away": 6}}}}}, (3, 6)),
        ({"overview": {"corners": {"home": 4, "away": 2}}}, (4, 2)),
        ({"data": {"overview": {}}}, None),
        ({"data": {"overview": {"corner_kicks": {"all": {"home": None, "away": None}}}}}, None),
    ],
)
def test_corner_extraction_handles_the_real_shapes(payload, expected):
    """Both spellings observed, and a present-but-null block must read as absent rather than
    becoming a fabricated zero."""
    assert thestatsapi._corners_from_stats(payload) == expected


def test_the_backfill_only_asks_about_fixtures_old_enough_to_have_data():
    """A measured 404 at 3.4 hours means an earlier attempt is a call spent to be told nothing.
    Both remaining gaps on the day this shipped (4.1h and 9.1h old) were correctly skipped."""
    from app.workers.ingest_live_scores import (
        THESTATSAPI_LOOKBACK_DAYS,
        THESTATSAPI_MAX_PER_RUN,
        THESTATSAPI_MIN_AGE_HOURS,
    )

    assert THESTATSAPI_MIN_AGE_HOURS >= 4, "earlier than the measured 404 is a wasted call"
    assert THESTATSAPI_LOOKBACK_DAYS >= 5, "the measured upper bound was around five days"
    assert 0 < THESTATSAPI_MAX_PER_RUN <= 50


def test_the_fallback_never_overwrites_the_primary():
    """API-Football stays primary. This fills only a NULL, so the two sources can never
    silently disagree about the same fixture."""
    import inspect

    from app.workers import ingest_live_scores

    source = inspect.getsource(ingest_live_scores._backfill_corners_from_thestatsapi)
    assert "FixtureLiveState.home_corners.is_(None)" in source


def test_an_ambiguous_score_match_is_refused():
    """Matching is date + score, because two providers spell teams differently. If a window
    holds two matches with the same scoreline the join is refused rather than guessed."""
    import inspect

    source = inspect.getsource(thestatsapi.fetch_corners)
    assert "len(matches) != 1" in source


# --- season resolution: the bug this shipped with, and the fix ------------------------------


def test_season_is_resolved_from_the_kickoff_date_not_a_label():
    """THE BUG THIS SHIPPED WITH, caught by querying a real J1 fixture rather than by review.

    The first version matched TheStatsAPI's season NAME against our stored season string. That
    works for calendar leagues ("Veikkausliiga 2026") and fails for every autumn-spring one,
    which are named "J1 League 26/27" and "Premier League 26/27" -- most of the pool. It passed
    its first live test only because the fixtures it filled were all Veikkausliiga.

    Our own labels cannot rescue it either: API-Football calls the EPL's 2026-27 season 2026
    (start year) and the J1 League's 2026-27 season 2027 (end year). Verified live against both.

    start_year/end_year are structured, so the kickoff date decides and no label is parsed.
    """
    import inspect

    source = inspect.getsource(thestatsapi._season_id)
    assert "start_year" in source and "end_year" in source
    assert "kickoff.year" in source
    # The signature must not take a season label at all -- that is what made the bug possible.
    assert "season: str" not in inspect.signature(thestatsapi._season_id).parameters


def test_an_ambiguous_season_is_refused_rather_than_picked():
    """The J1 League ran a calendar 2026 season AND began a 26/27 season in the same year while
    switching to the European calendar, so two seasons can claim one date. Picking one would
    match confidently against a fixture from a different season entirely."""
    import inspect

    source = inspect.getsource(thestatsapi._season_id)
    assert "len(spanning) == 1" in source
