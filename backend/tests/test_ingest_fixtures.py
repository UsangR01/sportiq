"""app/workers/ingest_fixtures.py — real DB-backed tests for the pieces added/fixed while
wiring up score display and history backfill:
  - _upsert_live_state: real score storage, reusing FixtureLiveState for both in-progress and
    completed fixtures (previously nothing ever wrote to this table at all).
  - _maybe_settle_outcome: writes a real settled Outcome row once, idempotently, when a
    fixture completes (previously a documented gap — GET /history's own blocker).
  - The TeamFeatures duplicate-row bug: re-running _ingest_fixtures_for_league for the same
    not-yet-played fixture used to insert a brand-new TeamFeatures row every time with no
    dedup at all, which would eventually make _run_predictions.py's lookup raise
    MultipleResultsFound. Verified fixed via a fake adapter run twice.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete, select

from app.adapters.base import FixturePayload, TeamStats
from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team, TeamFeatures
from app.history.models import MatchResult, Outcome
from app.predictions.models import ConfidenceTier, Prediction
from app.sports.models import League, Sport
from app.workers.ingest_fixtures import (
    _ingest_fixtures,
    _ingest_fixtures_for_league,
    _maybe_fetch_corner_stats,
    _maybe_settle_outcome,
    _upsert_live_state,
)


@pytest.fixture
async def seeded_fixture():
    kickoff = datetime.now(UTC) - timedelta(hours=1)
    async with async_session_factory() as db:
        slug = f"test-sport-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Test Sport", model_type="test", active=True)
        db.add(sport)
        await db.flush()
        league = League(
            sport_id=sport.id,
            slug="test-league",
            name="Test League",
            country="XX",
            tier=1,
            active=True,
        )
        db.add(league)
        await db.flush()
        home = Team(sport_id=sport.id, league_id=league.id, name="Home FC", external_id="1")
        away = Team(sport_id=sport.id, league_id=league.id, name="Away FC", external_id="2")
        db.add_all([home, away])
        await db.flush()
        fixture = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            external_id="fx-live-1",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=kickoff,
            status=FixtureStatus.LIVE,
            season="2026",
        )
        db.add(fixture)
        await db.commit()
        await db.refresh(fixture)

    yield sport, fixture

    async with async_session_factory() as db:
        await db.execute(delete(Outcome).where(Outcome.fixture_id == fixture.id))
        await db.execute(delete(FixtureLiveState).where(FixtureLiveState.fixture_id == fixture.id))
        await db.execute(delete(Fixture).where(Fixture.sport_id == sport.id))
        await db.execute(delete(Team).where(Team.sport_id == sport.id))
        await db.execute(delete(League).where(League.sport_id == sport.id))
        await db.execute(delete(Sport).where(Sport.id == sport.id))
        await db.commit()


def make_payload(**overrides) -> FixturePayload:
    payload = dict(
        external_id="fx-live-1",
        league_external_id="test-league",
        home_team_external_id="1",
        away_team_external_id="2",
        kickoff_utc=datetime.now(UTC),
        season="2026",
        status="live",
        home_score=1,
        away_score=0,
        match_minute=60,
    )
    payload.update(overrides)
    return FixturePayload(**payload)


async def test_upsert_live_state_creates_new_row(seeded_fixture):
    _sport, fixture = seeded_fixture
    async with async_session_factory() as db:
        await _upsert_live_state(db, fixture.id, make_payload())
        await db.commit()

    async with async_session_factory() as db:
        row = (
            await db.execute(
                select(FixtureLiveState).where(FixtureLiveState.fixture_id == fixture.id)
            )
        ).scalar_one()
        assert row.home_score == 1
        assert row.away_score == 0
        assert row.match_minute == 60
        assert row.status == "live"


async def test_upsert_live_state_updates_existing_row(seeded_fixture):
    _sport, fixture = seeded_fixture
    async with async_session_factory() as db:
        await _upsert_live_state(
            db, fixture.id, make_payload(home_score=1, away_score=0, match_minute=60)
        )
        await db.commit()

    async with async_session_factory() as db:
        await _upsert_live_state(
            db, fixture.id, make_payload(home_score=2, away_score=0, match_minute=75)
        )
        await db.commit()

    async with async_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(FixtureLiveState).where(FixtureLiveState.fixture_id == fixture.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1  # updated in place, not a second row
        assert rows[0].home_score == 2
        assert rows[0].match_minute == 75


async def test_upsert_live_state_no_ops_without_scores(seeded_fixture):
    _sport, fixture = seeded_fixture
    async with async_session_factory() as db:
        await _upsert_live_state(
            db, fixture.id, make_payload(home_score=None, away_score=None, status="scheduled")
        )
        await db.commit()

    async with async_session_factory() as db:
        row = (
            await db.execute(
                select(FixtureLiveState).where(FixtureLiveState.fixture_id == fixture.id)
            )
        ).scalar_one_or_none()
        assert row is None


async def _get_teams(db, fixture):
    home = (await db.execute(select(Team).where(Team.id == fixture.home_team_id))).scalar_one()
    away = (await db.execute(select(Team).where(Team.id == fixture.away_team_id))).scalar_one()
    return home, away


async def test_maybe_settle_outcome_writes_real_result(seeded_fixture):
    sport, fixture = seeded_fixture
    async with async_session_factory() as db:
        home, away = await _get_teams(db, fixture)
        await _maybe_settle_outcome(
            db,
            fixture.id,
            make_payload(status="completed", home_score=3, away_score=1),
            home,
            away,
            sport.slug,
        )
        await db.commit()

    async with async_session_factory() as db:
        outcome = (
            await db.execute(select(Outcome).where(Outcome.fixture_id == fixture.id))
        ).scalar_one()
        assert outcome.home_score == 3
        assert outcome.away_score == 1
        assert outcome.result == MatchResult.HOME_WIN


async def test_maybe_settle_outcome_updates_elo(seeded_fixture):
    sport, fixture = seeded_fixture
    async with async_session_factory() as db:
        home, away = await _get_teams(db, fixture)
        assert home.elo_rating is None
        assert away.elo_rating is None
        await _maybe_settle_outcome(
            db,
            fixture.id,
            make_payload(status="completed", home_score=3, away_score=1),
            home,
            away,
            sport.slug,
        )
        await db.commit()

    async with async_session_factory() as db:
        home, away = await _get_teams(db, fixture)
        # Home team won as the pre-match favourite-by-default (both start at INITIAL_ELO, a
        # win against an equally-rated opponent always raises the winner's rating).
        assert home.elo_rating > 1500.0
        assert away.elo_rating < 1500.0


async def test_maybe_settle_outcome_is_idempotent(seeded_fixture):
    sport, fixture = seeded_fixture
    payload = make_payload(status="completed", home_score=2, away_score=2)
    async with async_session_factory() as db:
        home, away = await _get_teams(db, fixture)
        await _maybe_settle_outcome(db, fixture.id, payload, home, away, sport.slug)
        await db.commit()
    async with async_session_factory() as db:
        home, away = await _get_teams(db, fixture)
        elo_home_after_first = home.elo_rating
        # a second worker run observing the same completion
        await _maybe_settle_outcome(db, fixture.id, payload, home, away, sport.slug)
        await db.commit()

    async with async_session_factory() as db:
        outcomes = (
            (await db.execute(select(Outcome).where(Outcome.fixture_id == fixture.id)))
            .scalars()
            .all()
        )
        assert len(outcomes) == 1
        assert outcomes[0].result == MatchResult.DRAW
        home, _away = await _get_teams(db, fixture)
        # Elo must not be double-applied on the second, idempotent-no-op call.
        assert home.elo_rating == elo_home_after_first


async def test_maybe_settle_outcome_skips_non_completed(seeded_fixture):
    sport, fixture = seeded_fixture
    async with async_session_factory() as db:
        home, away = await _get_teams(db, fixture)
        await _maybe_settle_outcome(
            db,
            fixture.id,
            make_payload(status="live", home_score=1, away_score=0),
            home,
            away,
            sport.slug,
        )
        await db.commit()

    async with async_session_factory() as db:
        outcome = (
            await db.execute(select(Outcome).where(Outcome.fixture_id == fixture.id))
        ).scalar_one_or_none()
        assert outcome is None


class _FakeTeamForCorners:
    def __init__(self, external_id):
        self.external_id = external_id


async def test_maybe_fetch_corner_stats_football_stores_real_corners(monkeypatch):
    """Real per-team corner counts, football only — see
    app/adapters/api_football.py:fetch_corner_stats. Mocking only the external API boundary,
    per this project's established convention (see CLAUDE.md's test_push_token.py note)."""

    async def fake_fetch_corner_stats(fixture_external_id):
        assert fixture_external_id == "fx-live-1"
        return {"1": 7, "2": 4}

    monkeypatch.setattr("app.adapters.api_football.fetch_corner_stats", fake_fetch_corner_stats)

    home_corners, away_corners = await _maybe_fetch_corner_stats(
        "football",
        make_payload(status="completed", home_score=3, away_score=1),
        _FakeTeamForCorners("1"),
        _FakeTeamForCorners("2"),
    )
    assert home_corners == 7
    assert away_corners == 4


async def test_maybe_fetch_corner_stats_skips_non_football(monkeypatch):
    called = False

    async def fake_fetch_corner_stats(fixture_external_id):
        nonlocal called
        called = True
        return {"1": 7, "2": 4}

    monkeypatch.setattr("app.adapters.api_football.fetch_corner_stats", fake_fetch_corner_stats)

    result = await _maybe_fetch_corner_stats(
        "nba",
        make_payload(status="completed", home_score=3, away_score=1),
        _FakeTeamForCorners("1"),
        _FakeTeamForCorners("2"),
    )
    assert result == (None, None)
    assert called is False


async def test_maybe_fetch_corner_stats_degrades_gracefully_on_http_error(monkeypatch):
    async def fake_fetch_corner_stats(fixture_external_id):
        raise httpx.HTTPStatusError("boom", request=None, response=None)

    monkeypatch.setattr("app.adapters.api_football.fetch_corner_stats", fake_fetch_corner_stats)

    result = await _maybe_fetch_corner_stats(
        "football",
        make_payload(status="completed", home_score=3, away_score=1),
        _FakeTeamForCorners("1"),
        _FakeTeamForCorners("2"),
    )
    assert result == (None, None)


class FakeAdapter:
    """Minimal stand-in for a DataSourceAdapter — only the two methods
    _ingest_fixtures_for_league actually calls."""

    def __init__(self, fixture_payloads):
        self._fixture_payloads = fixture_payloads

    async def fetch_fixtures(self, sport, league, days_ahead, days_back=0):
        return self._fixture_payloads

    async def fetch_team_stats(self, team_id, n_matches, league=None):
        return TeamStats(team_external_id=team_id, form_pts_5=0.5, attack_str=1.0, defence_str=1.0)


async def test_rerunning_ingest_does_not_duplicate_team_features(monkeypatch):
    """The real bug: re-running this worker for the same not-yet-played fixture previously
    inserted a new TeamFeatures row every time (no dedup at all)."""
    kickoff = datetime.now(UTC) + timedelta(days=1)
    async with async_session_factory() as db:
        slug = f"test-sport-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Test Sport", model_type="test", active=True)
        db.add(sport)
        await db.flush()
        league = League(
            sport_id=sport.id,
            slug="test-league",
            name="Test League",
            country="XX",
            tier=1,
            active=True,
        )
        db.add(league)
        await db.commit()
        await db.refresh(sport)
        await db.refresh(league)

    payload = FixturePayload(
        external_id="fx-dedup-1",
        league_external_id="test-league",
        home_team_external_id="10",
        away_team_external_id="20",
        kickoff_utc=kickoff,
        season="2026",
        home_team_name="Home FC",
        away_team_name="Away FC",
        status="scheduled",
    )
    fake_adapter = FakeAdapter([payload])

    import app.adapters.factory as factory_module

    monkeypatch.setattr(
        factory_module.AdapterFactory, "get_stats_adapter", lambda slug: fake_adapter
    )

    try:
        await _ingest_fixtures_for_league(sport, league)
        await _ingest_fixtures_for_league(sport, league)  # simulates the next day's re-run

        async with async_session_factory() as db:
            fixture = (
                await db.execute(select(Fixture).where(Fixture.external_id == "fx-dedup-1"))
            ).scalar_one()
            rows = (
                (
                    await db.execute(
                        select(TeamFeatures).where(TeamFeatures.fixture_id == fixture.id)
                    )
                )
                .scalars()
                .all()
            )
            # One row per team (home + away), not one row per team per run.
            assert len(rows) == 2
    finally:
        async with async_session_factory() as db:
            fixture = (
                await db.execute(select(Fixture).where(Fixture.external_id == "fx-dedup-1"))
            ).scalar_one_or_none()
            if fixture is not None:
                await db.execute(delete(TeamFeatures).where(TeamFeatures.fixture_id == fixture.id))
                await db.execute(delete(Fixture).where(Fixture.id == fixture.id))
            await db.execute(delete(Team).where(Team.sport_id == sport.id))
            await db.execute(delete(League).where(League.sport_id == sport.id))
            await db.execute(delete(Sport).where(Sport.id == sport.id))
            await db.commit()


async def test_ingest_queues_a_prediction_for_a_fixture_that_has_none_yet(monkeypatch):
    """The real bug, reported by the user: "no prediction made at all" for most upcoming
    fixtures in newly-added leagues. run_predictions was only ever triggered by
    ingest_injuries.py's re-inference path (a real key-player status change within 3 hours of
    kickoff) - an ordinary freshly-ingested fixture never got an initial prediction at all.
    Fixed by queuing run_predictions.delay for any upcoming fixture with no Prediction row yet,
    and ONLY that case - re-running this worker daily must never re-queue a fixture that
    already has a real prediction (would waste real H2H/moneyline API calls for no reason)."""
    kickoff = datetime.now(UTC) + timedelta(days=1)
    async with async_session_factory() as db:
        slug = f"test-sport-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Test Sport", model_type="test", active=True)
        db.add(sport)
        await db.flush()
        league = League(
            sport_id=sport.id,
            slug="test-league",
            name="Test League",
            country="XX",
            tier=1,
            active=True,
        )
        db.add(league)
        await db.commit()
        await db.refresh(sport)
        await db.refresh(league)

    payload = FixturePayload(
        external_id="fx-needs-prediction-1",
        league_external_id="test-league",
        home_team_external_id="30",
        away_team_external_id="40",
        kickoff_utc=kickoff,
        season="2026",
        home_team_name="Home FC",
        away_team_name="Away FC",
        status="scheduled",
    )
    fake_adapter = FakeAdapter([payload])

    import app.adapters.factory as factory_module

    monkeypatch.setattr(
        factory_module.AdapterFactory, "get_stats_adapter", lambda slug: fake_adapter
    )

    queued_fixture_ids: list[str] = []
    import app.workers.run_predictions as run_predictions_module

    monkeypatch.setattr(
        run_predictions_module.run_predictions,
        "delay",
        lambda fixture_id: queued_fixture_ids.append(fixture_id),
    )

    try:
        await _ingest_fixtures_for_league(sport, league)

        async with async_session_factory() as db:
            fixture = (
                await db.execute(
                    select(Fixture).where(Fixture.external_id == "fx-needs-prediction-1")
                )
            ).scalar_one()

        assert queued_fixture_ids == [str(fixture.id)]

        # Give it a real prediction, then re-run (simulates the next day) - must NOT re-queue.
        async with async_session_factory() as db:
            db.add(
                Prediction(
                    fixture_id=fixture.id,
                    model_version="test_model_v1",
                    home_prob=0.5,
                    draw_prob=0.3,
                    away_prob=0.2,
                    confidence_tier=ConfidenceTier.MEDIUM,
                    created_at=datetime.now(UTC),
                )
            )
            await db.commit()

        queued_fixture_ids.clear()
        await _ingest_fixtures_for_league(sport, league)
        assert queued_fixture_ids == []
    finally:
        async with async_session_factory() as db:
            fixture = (
                await db.execute(
                    select(Fixture).where(Fixture.external_id == "fx-needs-prediction-1")
                )
            ).scalar_one_or_none()
            if fixture is not None:
                await db.execute(delete(Prediction).where(Prediction.fixture_id == fixture.id))
                await db.execute(delete(TeamFeatures).where(TeamFeatures.fixture_id == fixture.id))
                await db.execute(delete(Fixture).where(Fixture.id == fixture.id))
            await db.execute(delete(Team).where(Team.sport_id == sport.id))
            await db.execute(delete(League).where(League.sport_id == sport.id))
            await db.execute(delete(Sport).where(Sport.id == sport.id))
            await db.commit()


class _RaisingAdapter:
    """Stands in for a stats adapter whose provider tier isn't unlocked yet (e.g. tennis's
    BallDontLie endpoints 401ing until ALL-STAR is confirmed) — real HTTPError, not a
    ValueError (ingest_odds.py's own per-adapter isolation only ever catches ValueError,
    a different failure shape)."""

    async def fetch_fixtures(self, sport, league, days_ahead, days_back=0):
        raise httpx.HTTPStatusError("boom", request=None, response=None)

    async def fetch_team_stats(self, team_id, n_matches, league=None):
        raise httpx.HTTPStatusError("boom", request=None, response=None)


class _NoOpAdapter:
    """_ingest_fixtures() loops over EVERY active Sport/League in the whole DB, not just this
    test's own two synthetic rows — real nba/football/tennis rows are already seeded in the
    dev DB this test suite runs against. Every real sport slug must get an adapter that
    returns zero fixtures, or this test would create real, bogus Team/Fixture rows against
    live seeded data (a real mistake made once while writing this test — see git history)."""

    async def fetch_fixtures(self, sport, league, days_ahead, days_back=0):
        return []

    async def fetch_team_stats(self, team_id, n_matches, league=None):
        return TeamStats(team_external_id=team_id)


async def test_ingest_fixtures_isolates_one_sports_adapter_failure(monkeypatch):
    """The real regression: adding tennis (whose BallDontLie adapter 401s until the ALL-STAR
    plan is confirmed) silently froze EVERY other sport's daily fixture ingest, since
    _ingest_fixtures()'s sport/league loop had no per-league exception isolation at all — one
    HTTPError killed the whole run. Reported by the user as a Scottish Premiership match
    stuck showing a stale live minute long after the real game had finished."""
    kickoff = datetime.now(UTC) + timedelta(days=1)
    async with async_session_factory() as db:
        broken_slug = f"test-broken-{uuid.uuid4().hex[:8]}"
        healthy_slug = f"test-healthy-{uuid.uuid4().hex[:8]}"
        broken_sport = Sport(slug=broken_slug, name="Broken Sport", model_type="test", active=True)
        healthy_sport = Sport(
            slug=healthy_slug, name="Healthy Sport", model_type="test", active=True
        )
        db.add_all([broken_sport, healthy_sport])
        await db.flush()
        broken_league = League(
            sport_id=broken_sport.id,
            slug="broken-league",
            name="Broken League",
            country="XX",
            tier=1,
            active=True,
        )
        healthy_league = League(
            sport_id=healthy_sport.id,
            slug="healthy-league",
            name="Healthy League",
            country="XX",
            tier=1,
            active=True,
        )
        db.add_all([broken_league, healthy_league])
        await db.commit()
        await db.refresh(broken_sport)
        await db.refresh(healthy_sport)

    payload = FixturePayload(
        external_id="fx-isolation-1",
        league_external_id="healthy-league",
        home_team_external_id="50",
        away_team_external_id="60",
        kickoff_utc=kickoff,
        season="2026",
        home_team_name="Home FC",
        away_team_name="Away FC",
        status="scheduled",
    )
    healthy_adapter = FakeAdapter([payload])
    broken_adapter = _RaisingAdapter()
    noop_adapter = _NoOpAdapter()

    import app.adapters.factory as factory_module

    def _fake_get_stats_adapter(slug):
        if slug == broken_slug:
            return broken_adapter
        if slug == healthy_slug:
            return healthy_adapter
        return noop_adapter  # every real sport already seeded in the dev DB — must no-op

    monkeypatch.setattr(factory_module.AdapterFactory, "get_stats_adapter", _fake_get_stats_adapter)

    try:
        await _ingest_fixtures()  # must not raise, despite the broken sport in the same run

        async with async_session_factory() as db:
            fixture = (
                await db.execute(
                    select(Fixture).where(
                        Fixture.sport_id == healthy_sport.id,
                        Fixture.external_id == "fx-isolation-1",
                    )
                )
            ).scalar_one_or_none()
            # The healthy sport's fixture was still ingested despite the other sport's
            # adapter blowing up earlier/later in the same loop.
            assert fixture is not None
    finally:
        async with async_session_factory() as db:
            fixture = (
                await db.execute(
                    select(Fixture).where(
                        Fixture.sport_id == healthy_sport.id,
                        Fixture.external_id == "fx-isolation-1",
                    )
                )
            ).scalar_one_or_none()
            if fixture is not None:
                await db.execute(delete(TeamFeatures).where(TeamFeatures.fixture_id == fixture.id))
                await db.execute(delete(Prediction).where(Prediction.fixture_id == fixture.id))
                await db.execute(delete(Fixture).where(Fixture.id == fixture.id))
            await db.execute(
                delete(Team).where(Team.sport_id.in_([broken_sport.id, healthy_sport.id]))
            )
            await db.execute(
                delete(League).where(League.sport_id.in_([broken_sport.id, healthy_sport.id]))
            )
            await db.execute(delete(Sport).where(Sport.id.in_([broken_sport.id, healthy_sport.id])))
            await db.commit()


class _UnregisteredAdapter:
    """AdapterFactory.get_stats_adapter's real behavior for a sport with no registered
    adapter at all — a ValueError, not an httpx.HTTPError."""

    async def fetch_fixtures(self, sport, league, days_ahead, days_back=0):
        raise ValueError(f"No stats adapter registered for data_source_slug={sport!r}")

    async def fetch_team_stats(self, team_id, n_matches, league=None):
        raise ValueError("no adapter")


async def test_ingest_fixtures_isolates_one_sports_unregistered_adapter(monkeypatch):
    """Same isolation guarantee, for the OTHER real failure shape: a sport with no stats
    adapter registered at all raises ValueError (see AdapterFactory.get_stats_adapter), not
    an httpx.HTTPError — must be isolated the same way."""
    kickoff = datetime.now(UTC) + timedelta(days=1)
    async with async_session_factory() as db:
        broken_slug = f"test-unregistered-{uuid.uuid4().hex[:8]}"
        healthy_slug = f"test-healthy2-{uuid.uuid4().hex[:8]}"
        broken_sport = Sport(
            slug=broken_slug, name="Unregistered Sport", model_type="test", active=True
        )
        healthy_sport = Sport(
            slug=healthy_slug, name="Healthy Sport 2", model_type="test", active=True
        )
        db.add_all([broken_sport, healthy_sport])
        await db.flush()
        broken_league = League(
            sport_id=broken_sport.id,
            slug="unregistered-league",
            name="Unregistered League",
            country="XX",
            tier=1,
            active=True,
        )
        healthy_league = League(
            sport_id=healthy_sport.id,
            slug="healthy-league-2",
            name="Healthy League 2",
            country="XX",
            tier=1,
            active=True,
        )
        db.add_all([broken_league, healthy_league])
        await db.commit()
        await db.refresh(broken_sport)
        await db.refresh(healthy_sport)

    payload = FixturePayload(
        external_id="fx-isolation-2",
        league_external_id="healthy-league-2",
        home_team_external_id="70",
        away_team_external_id="80",
        kickoff_utc=kickoff,
        season="2026",
        home_team_name="Home FC 2",
        away_team_name="Away FC 2",
        status="scheduled",
    )
    healthy_adapter = FakeAdapter([payload])
    broken_adapter = _UnregisteredAdapter()
    noop_adapter = _NoOpAdapter()

    import app.adapters.factory as factory_module

    def _fake_get_stats_adapter(slug):
        if slug == broken_slug:
            return broken_adapter
        if slug == healthy_slug:
            return healthy_adapter
        return noop_adapter

    monkeypatch.setattr(factory_module.AdapterFactory, "get_stats_adapter", _fake_get_stats_adapter)

    try:
        await _ingest_fixtures()  # must not raise, despite the unregistered sport in the run

        async with async_session_factory() as db:
            fixture = (
                await db.execute(
                    select(Fixture).where(
                        Fixture.sport_id == healthy_sport.id,
                        Fixture.external_id == "fx-isolation-2",
                    )
                )
            ).scalar_one_or_none()
            assert fixture is not None
    finally:
        async with async_session_factory() as db:
            fixture = (
                await db.execute(
                    select(Fixture).where(
                        Fixture.sport_id == healthy_sport.id,
                        Fixture.external_id == "fx-isolation-2",
                    )
                )
            ).scalar_one_or_none()
            if fixture is not None:
                await db.execute(delete(TeamFeatures).where(TeamFeatures.fixture_id == fixture.id))
                await db.execute(delete(Prediction).where(Prediction.fixture_id == fixture.id))
                await db.execute(delete(Fixture).where(Fixture.id == fixture.id))
            await db.execute(
                delete(Team).where(Team.sport_id.in_([broken_sport.id, healthy_sport.id]))
            )
            await db.execute(
                delete(League).where(League.sport_id.in_([broken_sport.id, healthy_sport.id]))
            )
            await db.execute(delete(Sport).where(Sport.id.in_([broken_sport.id, healthy_sport.id])))
            await db.commit()


async def test_a_withdrawn_fixture_is_flagged_and_unflagged_by_the_real_ingest_path(monkeypatch):
    """The full round trip for Fixture.withdrawn, through _ingest_fixtures_for_league itself.

    The reconciliation is tested in isolation in test_vanished_fixtures_reconcile.py. What that
    cannot cover is the CLEARING half, which lives in a different function (the update branch
    of this worker) and is the trap: a fixture that can be flagged but never unflagged is
    hidden from the feed forever, and a withdrawn draw genuinely can be republished.
    """
    kickoff = datetime.now(UTC) + timedelta(days=1)
    async with async_session_factory() as db:
        slug = f"test-sport-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Test Sport", model_type="test", active=True)
        db.add(sport)
        await db.flush()
        league = League(sport_id=sport.id, slug="wd-league", name="L", country="XX", tier=1)
        db.add(league)
        await db.commit()
        await db.refresh(sport)
        await db.refresh(league)

    def payload(external_id, home, away):
        return FixturePayload(
            external_id=external_id,
            league_external_id="wd-league",
            home_team_external_id=home,
            away_team_external_id=away,
            kickoff_utc=kickoff,
            season="2026",
            home_team_name=f"Team {home}",
            away_team_name=f"Team {away}",
            status="scheduled",
        )

    kept = payload("fx-wd-kept", "10", "20")
    withdrawn = payload("fx-wd-gone", "30", "40")

    import app.adapters.factory as factory_module

    def use(payloads):
        monkeypatch.setattr(
            factory_module.AdapterFactory, "get_stats_adapter", lambda s: FakeAdapter(payloads)
        )

    async def state(external_id):
        async with async_session_factory() as db:
            row = (
                await db.execute(
                    select(Fixture.status, Fixture.withdrawn).where(
                        Fixture.external_id == external_id
                    )
                )
            ).one()
            return row[0], row[1]

    try:
        use([kept, withdrawn])
        await _ingest_fixtures_for_league(sport, league)
        assert await state("fx-wd-gone") == (FixtureStatus.SCHEDULED, False)

        # The provider drops it from a date it still reports on — the reported case.
        use([kept])
        await _ingest_fixtures_for_league(sport, league)
        assert await state("fx-wd-gone") == (FixtureStatus.POSTPONED, True)
        # The fixture still listed must be untouched, or the sweep is worse than the bug.
        assert await state("fx-wd-kept") == (FixtureStatus.SCHEDULED, False)

        # Republished: it must come back, not stay invisible.
        use([kept, withdrawn])
        await _ingest_fixtures_for_league(sport, league)
        assert await state("fx-wd-gone") == (FixtureStatus.SCHEDULED, False)
    finally:
        async with async_session_factory() as db:
            ids = (
                (await db.execute(select(Fixture.id).where(Fixture.sport_id == sport.id)))
                .scalars()
                .all()
            )
            if ids:
                await db.execute(delete(TeamFeatures).where(TeamFeatures.fixture_id.in_(ids)))
            await db.execute(delete(Fixture).where(Fixture.sport_id == sport.id))
            await db.execute(delete(Team).where(Team.sport_id == sport.id))
            await db.execute(delete(League).where(League.sport_id == sport.id))
            await db.execute(delete(Sport).where(Sport.id == sport.id))
            await db.commit()


async def test_a_prediction_from_a_superseded_model_is_regenerated(monkeypatch):
    """A retrain that reaches nobody is not a retrain.

    Measured 2026-08-13: all 135 upcoming football fixtures were serving predictions from one
    of FOUR superseded model versions, the newest three retrains old — because the queueing
    guard only ever asked "does a prediction exist?". Promotion is a DB update rather than a
    deploy, so nothing anywhere noticed; the numbers on every card simply stayed old.

    The guard against the opposite error still has to hold: matching versions must NOT re-queue,
    or every daily run spends real H2H/odds API calls recomputing predictions nothing changed.
    """
    from app.predictions.models import ModelRegistry

    kickoff = datetime.now(UTC) + timedelta(days=1)
    async with async_session_factory() as db:
        slug = f"test-sport-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Test Sport", model_type="test", active=True)
        db.add(sport)
        await db.flush()
        league = League(sport_id=sport.id, slug="stale-l", name="L", country="XX", tier=1)
        db.add(league)
        db.add(
            ModelRegistry(
                sport_id=sport.id,
                version="active_v2",
                artefact_path="/nowhere.joblib",
                is_active=True,
                trained_at=datetime.now(UTC),
            )
        )
        await db.commit()
        await db.refresh(sport)
        await db.refresh(league)

    payload = FixturePayload(
        external_id="fx-stale-model-1",
        league_external_id="stale-l",
        home_team_external_id="50",
        away_team_external_id="60",
        kickoff_utc=kickoff,
        season="2026",
        home_team_name="Home FC",
        away_team_name="Away FC",
        status="scheduled",
    )

    import app.adapters.factory as factory_module

    monkeypatch.setattr(
        factory_module.AdapterFactory, "get_stats_adapter", lambda s: FakeAdapter([payload])
    )
    queued: list[str] = []
    import app.workers.run_predictions as run_predictions_module

    monkeypatch.setattr(
        run_predictions_module.run_predictions, "delay", lambda fid: queued.append(fid)
    )

    async def set_prediction_version(fixture_id, version):
        async with async_session_factory() as db:
            await db.execute(delete(Prediction).where(Prediction.fixture_id == fixture_id))
            db.add(
                Prediction(
                    fixture_id=fixture_id,
                    model_version=version,
                    home_prob=0.5,
                    draw_prob=0.3,
                    away_prob=0.2,
                    confidence_tier=ConfidenceTier.MEDIUM,
                    created_at=datetime.now(UTC),
                )
            )
            await db.commit()

    try:
        await _ingest_fixtures_for_league(sport, league)
        async with async_session_factory() as db:
            fixture = (
                await db.execute(select(Fixture).where(Fixture.external_id == "fx-stale-model-1"))
            ).scalar_one()

        # Superseded: must be regenerated.
        await set_prediction_version(fixture.id, "old_v1")
        queued.clear()
        await _ingest_fixtures_for_league(sport, league)
        assert queued == [str(fixture.id)]

        # Current: must NOT be, or a daily run burns real API calls for nothing.
        await set_prediction_version(fixture.id, "active_v2")
        queued.clear()
        await _ingest_fixtures_for_league(sport, league)
        assert queued == []
    finally:
        async with async_session_factory() as db:
            ids = (
                (await db.execute(select(Fixture.id).where(Fixture.sport_id == sport.id)))
                .scalars()
                .all()
            )
            if ids:
                await db.execute(delete(Prediction).where(Prediction.fixture_id.in_(ids)))
                await db.execute(delete(TeamFeatures).where(TeamFeatures.fixture_id.in_(ids)))
            await db.execute(delete(Fixture).where(Fixture.sport_id == sport.id))
            await db.execute(delete(ModelRegistry).where(ModelRegistry.sport_id == sport.id))
            await db.execute(delete(Team).where(Team.sport_id == sport.id))
            await db.execute(delete(League).where(League.sport_id == sport.id))
            await db.execute(delete(Sport).where(Sport.id == sport.id))
            await db.commit()


async def test_a_redrawn_fixture_updates_its_players(monkeypatch):
    """THE REPORTED BUG: "the players don't match the games for today".

    A qualifying draw is published provisionally and filled in as earlier rounds settle, and
    BallDontLie updates the EXISTING record rather than issuing a new one. Team ids were written
    on INSERT only, so whichever pairing we saw first stuck forever — measured against the live
    provider, we showed Majchrzak v Jarry where it had Majchrzak v BONZI, and Comesana v
    Bellucci where it had Comesana v YIBING WU.

    _reconcile_vanished_fixtures structurally cannot catch this: the id is still in the
    payload, so the fixture has not vanished. Only the update path can.
    """
    kickoff = datetime.now(UTC) + timedelta(days=1)
    async with async_session_factory() as db:
        sport = Sport(
            slug=f"test-tennis-{uuid.uuid4().hex[:8]}", name="Tennis", model_type="t", active=True
        )
        db.add(sport)
        await db.flush()
        league = League(
            sport_id=sport.id, slug="atp", name="ATP", country=None, tier=1, active=True
        )
        db.add(league)
        await db.commit()
        await db.refresh(sport)
        await db.refresh(league)

    def payload(away_external: str, away_name: str) -> FixturePayload:
        return FixturePayload(
            external_id="atp:redraw-1",
            league_external_id="atp",
            home_team_external_id="atp:p-home",
            away_team_external_id=away_external,
            kickoff_utc=kickoff,
            season="2026",
            home_team_name="Kamil Majchrzak",
            away_team_name=away_name,
            status="scheduled",
        )

    import app.adapters.factory as factory_module

    async def players() -> tuple[str, str]:
        async with async_session_factory() as db:
            fx = (
                await db.execute(select(Fixture).where(Fixture.external_id == "atp:redraw-1"))
            ).scalar_one()
            home = (await db.execute(select(Team).where(Team.id == fx.home_team_id))).scalar_one()
            away = (await db.execute(select(Team).where(Team.id == fx.away_team_id))).scalar_one()
            return home.name, away.name

    try:
        monkeypatch.setattr(
            factory_module.AdapterFactory,
            "get_stats_adapter",
            lambda slug: FakeAdapter([payload("atp:p-jarry", "Nicolas Jarry")]),
        )
        await _ingest_fixtures_for_league(sport, league)
        assert (await players())[1] == "Nicolas Jarry"

        # The provider fills the real opponent in under the SAME match id.
        monkeypatch.setattr(
            factory_module.AdapterFactory,
            "get_stats_adapter",
            lambda slug: FakeAdapter([payload("atp:p-bonzi", "Benjamin Bonzi")]),
        )
        await _ingest_fixtures_for_league(sport, league)

        home_name, away_name = await players()
        assert home_name == "Kamil Majchrzak", "the unchanged side must stay put"
        assert away_name == "Benjamin Bonzi", "the redrawn opponent must be picked up"
    finally:
        async with async_session_factory() as db:
            ids = (
                (await db.execute(select(Fixture.id).where(Fixture.sport_id == sport.id)))
                .scalars()
                .all()
            )
            if ids:
                # Children first: team_features and predictions both reference the fixture.
                await db.execute(delete(TeamFeatures).where(TeamFeatures.fixture_id.in_(ids)))
                await db.execute(delete(Prediction).where(Prediction.fixture_id.in_(ids)))
                await db.execute(
                    delete(FixtureLiveState).where(FixtureLiveState.fixture_id.in_(ids))
                )
            await db.execute(delete(Fixture).where(Fixture.sport_id == sport.id))
            await db.execute(delete(Team).where(Team.sport_id == sport.id))
            await db.execute(delete(League).where(League.sport_id == sport.id))
            await db.execute(delete(Sport).where(Sport.id == sport.id))
            await db.commit()
