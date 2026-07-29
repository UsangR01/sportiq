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

import pytest
from sqlalchemy import delete, select

from app.adapters.base import FixturePayload, TeamStats
from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team, TeamFeatures
from app.history.models import MatchResult, Outcome
from app.sports.models import League, Sport
from app.workers.ingest_fixtures import (
    _ingest_fixtures_for_league,
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
    _sport, fixture = seeded_fixture
    async with async_session_factory() as db:
        home, away = await _get_teams(db, fixture)
        await _maybe_settle_outcome(
            db, fixture.id, make_payload(status="completed", home_score=3, away_score=1), home, away
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
    _sport, fixture = seeded_fixture
    async with async_session_factory() as db:
        home, away = await _get_teams(db, fixture)
        assert home.elo_rating is None
        assert away.elo_rating is None
        await _maybe_settle_outcome(
            db, fixture.id, make_payload(status="completed", home_score=3, away_score=1), home, away
        )
        await db.commit()

    async with async_session_factory() as db:
        home, away = await _get_teams(db, fixture)
        # Home team won as the pre-match favourite-by-default (both start at INITIAL_ELO, a
        # win against an equally-rated opponent always raises the winner's rating).
        assert home.elo_rating > 1500.0
        assert away.elo_rating < 1500.0


async def test_maybe_settle_outcome_is_idempotent(seeded_fixture):
    _sport, fixture = seeded_fixture
    payload = make_payload(status="completed", home_score=2, away_score=2)
    async with async_session_factory() as db:
        home, away = await _get_teams(db, fixture)
        await _maybe_settle_outcome(db, fixture.id, payload, home, away)
        await db.commit()
    async with async_session_factory() as db:
        home, away = await _get_teams(db, fixture)
        elo_home_after_first = home.elo_rating
        # a second worker run observing the same completion
        await _maybe_settle_outcome(db, fixture.id, payload, home, away)
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
    _sport, fixture = seeded_fixture
    async with async_session_factory() as db:
        home, away = await _get_teams(db, fixture)
        await _maybe_settle_outcome(
            db, fixture.id, make_payload(status="live", home_score=1, away_score=0), home, away
        )
        await db.commit()

    async with async_session_factory() as db:
        outcome = (
            await db.execute(select(Outcome).where(Outcome.fixture_id == fixture.id))
        ).scalar_one_or_none()
        assert outcome is None


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
