"""app/workers/ingest_live_scores.py — regression test for a real bug: adding tennis (whose
BallDontLie adapter 401s until the ALL-STAR plan is confirmed, see CLAUDE.md) silently froze
live-score polling for EVERY other sport, since _ingest_live_scores()'s sport/league loop had
no per-league exception isolation at all — one HTTPError killed the whole 5-minute run.
Reported by the user as a Scottish Premiership match stuck showing a stale live minute long
after the real game had finished. Fixed with the same per-league isolation principle
ingest_odds.py already applies per-adapter (there, scoped to ValueError; here, to
httpx.HTTPError, the real failure shape a stats adapter raises)."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.history.models import Outcome
from app.sports.models import League, Sport
from app.workers.ingest_live_scores import _ingest_live_scores


class _RaisingAdapter:
    async def fetch_fixtures(self, sport, league, days_ahead, days_back=0):
        raise httpx.HTTPStatusError("boom", request=None, response=None)


class _HealthyAdapter:
    def __init__(self, payloads):
        self._payloads = payloads

    async def fetch_fixtures(self, sport, league, days_ahead, days_back=0):
        return self._payloads


async def test_ingest_live_scores_isolates_one_sports_adapter_failure(monkeypatch):
    kickoff = datetime.now(UTC) - timedelta(minutes=14)
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
        await db.flush()

        # A real fixture stuck mid-match, exactly like the user's report — this must get its
        # live status refreshed even though the OTHER sport's adapter blows up in the same run.
        home = Team(
            sport_id=healthy_sport.id, league_id=healthy_league.id, name="Home FC", external_id="1"
        )
        away = Team(
            sport_id=healthy_sport.id, league_id=healthy_league.id, name="Away FC", external_id="2"
        )
        db.add_all([home, away])
        await db.flush()
        fixture = Fixture(
            sport_id=healthy_sport.id,
            league_id=healthy_league.id,
            external_id="fx-live-isolation-1",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=kickoff,
            status=FixtureStatus.LIVE,
            season="2026",
        )
        db.add(fixture)
        await db.commit()
        await db.refresh(broken_sport)
        await db.refresh(healthy_sport)
        await db.refresh(fixture)

    from app.adapters.base import FixturePayload

    payload = FixturePayload(
        external_id="fx-live-isolation-1",
        league_external_id="healthy-league",
        home_team_external_id="1",
        away_team_external_id="2",
        kickoff_utc=kickoff,
        season="2026",
        status="completed",
        home_score=2,
        away_score=1,
    )
    healthy_adapter = _HealthyAdapter([payload])
    broken_adapter = _RaisingAdapter()

    import app.adapters.factory as factory_module

    monkeypatch.setattr(
        factory_module.AdapterFactory,
        "get_stats_adapter",
        lambda slug: broken_adapter if slug == broken_slug else healthy_adapter,
    )

    try:
        await _ingest_live_scores()  # must not raise, despite the broken sport in the same run

        async with async_session_factory() as db:
            updated = (
                await db.execute(select(Fixture).where(Fixture.id == fixture.id))
            ).scalar_one()
            # The healthy sport's real, stuck-live fixture was still refreshed to its real,
            # final status/score despite the other sport's adapter blowing up in the same loop.
            assert updated.status == FixtureStatus.COMPLETED
            live_state = (
                await db.execute(
                    select(FixtureLiveState).where(FixtureLiveState.fixture_id == fixture.id)
                )
            ).scalar_one()
            assert live_state.home_score == 2
            assert live_state.away_score == 1
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Outcome).where(Outcome.fixture_id == fixture.id))
            await db.execute(
                delete(FixtureLiveState).where(FixtureLiveState.fixture_id == fixture.id)
            )
            await db.execute(
                delete(Fixture).where(Fixture.sport_id.in_([broken_sport.id, healthy_sport.id]))
            )
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
    adapter at all — a ValueError, not an httpx.HTTPError. Must be isolated the same way."""

    async def fetch_fixtures(self, sport, league, days_ahead, days_back=0):
        raise ValueError(f"No stats adapter registered for data_source_slug={sport!r}")


async def test_ingest_live_scores_isolates_one_sports_unregistered_adapter(monkeypatch):
    kickoff = datetime.now(UTC) - timedelta(minutes=14)
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
        await db.flush()

        home = Team(
            sport_id=healthy_sport.id, league_id=healthy_league.id, name="Home FC", external_id="3"
        )
        away = Team(
            sport_id=healthy_sport.id, league_id=healthy_league.id, name="Away FC", external_id="4"
        )
        db.add_all([home, away])
        await db.flush()
        fixture = Fixture(
            sport_id=healthy_sport.id,
            league_id=healthy_league.id,
            external_id="fx-live-isolation-2",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=kickoff,
            status=FixtureStatus.LIVE,
            season="2026",
        )
        db.add(fixture)
        await db.commit()
        await db.refresh(broken_sport)
        await db.refresh(healthy_sport)
        await db.refresh(fixture)

    from app.adapters.base import FixturePayload

    payload = FixturePayload(
        external_id="fx-live-isolation-2",
        league_external_id="healthy-league-2",
        home_team_external_id="3",
        away_team_external_id="4",
        kickoff_utc=kickoff,
        season="2026",
        status="completed",
        home_score=1,
        away_score=0,
    )
    healthy_adapter = _HealthyAdapter([payload])
    broken_adapter = _UnregisteredAdapter()

    import app.adapters.factory as factory_module

    monkeypatch.setattr(
        factory_module.AdapterFactory,
        "get_stats_adapter",
        lambda slug: broken_adapter if slug == broken_slug else healthy_adapter,
    )

    try:
        await _ingest_live_scores()  # must not raise, despite the unregistered sport

        async with async_session_factory() as db:
            updated = (
                await db.execute(select(Fixture).where(Fixture.id == fixture.id))
            ).scalar_one()
            assert updated.status == FixtureStatus.COMPLETED
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Outcome).where(Outcome.fixture_id == fixture.id))
            await db.execute(
                delete(FixtureLiveState).where(FixtureLiveState.fixture_id == fixture.id)
            )
            await db.execute(
                delete(Fixture).where(Fixture.sport_id.in_([broken_sport.id, healthy_sport.id]))
            )
            await db.execute(
                delete(Team).where(Team.sport_id.in_([broken_sport.id, healthy_sport.id]))
            )
            await db.execute(
                delete(League).where(League.sport_id.in_([broken_sport.id, healthy_sport.id]))
            )
            await db.execute(delete(Sport).where(Sport.id.in_([broken_sport.id, healthy_sport.id])))
            await db.commit()
