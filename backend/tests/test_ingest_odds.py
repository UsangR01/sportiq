"""app/workers/ingest_odds.py:_resolve_fixture against a real Postgres connection — same
DB-touching pattern as test_fixture_matching.py. Covers the new Fixture.external_id fast path
(real whenever the odds provider and the stats/fixtures provider are the same, e.g.
API-Football odds for a football fixture API-Football itself ingested) alongside the existing
odds_provider_external_id and fuzzy-match paths, proving all three still cooperate correctly."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete

from app.adapters.base import OddsPayload
from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.sports.models import League, Sport
from app.workers.ingest_odds import _resolve_fixture


@pytest.fixture
async def seeded_fixture():
    kickoff = datetime(2026, 8, 22, 14, 0, tzinfo=UTC)

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

        home = Team(
            sport_id=sport.id, league_id=league.id, name="Home", short_name="HOM", external_id="1"
        )
        away = Team(
            sport_id=sport.id, league_id=league.id, name="Away", short_name="AWY", external_id="2"
        )
        db.add_all([home, away])
        await db.flush()

        fixture = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            external_id="1492316",  # same-provider fixture id (e.g. API-Football's own)
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=kickoff,
            status=FixtureStatus.SCHEDULED,
            season="2026",
        )
        db.add(fixture)
        await db.commit()
        await db.refresh(sport)
        await db.refresh(league)
        await db.refresh(fixture)

    yield sport, league, fixture

    async with async_session_factory() as db:
        await db.execute(delete(Fixture).where(Fixture.sport_id == sport.id))
        await db.execute(delete(Team).where(Team.sport_id == sport.id))
        await db.execute(delete(League).where(League.sport_id == sport.id))
        await db.execute(delete(Sport).where(Sport.id == sport.id))
        await db.commit()


async def test_resolves_by_matching_fixture_external_id(seeded_fixture):
    # Same-provider odds (e.g. API-Football's own fixture id) match directly, no team
    # abbreviation or kickoff time needed at all.
    sport, league, fixture = seeded_fixture
    payload = OddsPayload(
        fixture_external_id="1492316",
        bookmaker="Bet365",
        market="h2h",
        home_odds=1.80,
        draw_odds=3.50,
        away_odds=5.00,
        updated_at=datetime.now(UTC),
    )
    async with async_session_factory() as db:
        resolved = await _resolve_fixture(db, sport.id, league.id, payload)
        assert resolved is not None
        assert resolved.id == fixture.id
        await db.commit()

    async with async_session_factory() as db:
        refreshed = (
            await db.execute(Fixture.__table__.select().where(Fixture.id == fixture.id))
        ).first()
        assert refreshed.odds_provider_external_id == "1492316"


async def test_no_match_falls_through_to_none_for_unmatched_provider_id(seeded_fixture):
    # A different-ID-space provider's id (no team names to fuzzy-match against here) simply
    # doesn't resolve — never guesses.
    sport, league, _fixture = seeded_fixture
    payload = OddsPayload(
        fixture_external_id="some-other-provider-event-id",
        bookmaker="BetMGM",
        market="h2h",
        home_odds=2.0,
        draw_odds=None,
        away_odds=1.9,
        updated_at=datetime.now(UTC),
    )
    async with async_session_factory() as db:
        resolved = await _resolve_fixture(db, sport.id, league.id, payload)
    assert resolved is None
