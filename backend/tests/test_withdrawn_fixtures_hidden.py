"""GET /fixtures hides a WITHDRAWN fixture but still shows a called-off one.

Both are POSTPONED, and that shared bucket is deliberate — but they mean different things to a
user. A provider reporting PST/CANC is saying a real scheduled match was called off, which is
worth showing. A fixture that silently disappeared from the provider's list before its kickoff
was, in the case this was built for, never a real scheduled match: BallDontLie published a
provisional Cincinnati draw, withdrew it, and replaced it with different matches. That put 33
grey cards on one day and buried its two genuine picks underneath them.

The distinction is the whole point, so the test that matters most is the SECOND one — hiding
must not quietly swallow real postponements, which users have already asked to see.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.main import app
from app.sports.models import League, Sport


@pytest.fixture
async def api_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def world():
    kickoff = datetime.now(UTC) + timedelta(days=1)
    slug = f"test-sport-{uuid.uuid4().hex[:8]}"
    async with async_session_factory() as db:
        sport = Sport(slug=slug, name="Test Sport", model_type="test", active=True)
        db.add(sport)
        await db.flush()
        league = League(sport_id=sport.id, slug="wd-l", name="L", country="XX", tier=1)
        db.add(league)
        await db.flush()
        home = Team(sport_id=sport.id, league_id=league.id, name="Home FC", external_id="1")
        away = Team(sport_id=sport.id, league_id=league.id, name="Away FC", external_id="2")
        db.add_all([home, away])
        await db.flush()

        def make(tag, status, withdrawn):
            return Fixture(
                sport_id=sport.id,
                league_id=league.id,
                external_id=f"{tag}-{uuid.uuid4().hex[:6]}",
                home_team_id=home.id,
                away_team_id=away.id,
                kickoff_utc=kickoff,
                status=status,
                season="2026",
                withdrawn=withdrawn,
            )

        db.add_all(
            [
                make("scheduled", FixtureStatus.SCHEDULED, False),
                make("calledoff", FixtureStatus.POSTPONED, False),
                make("withdrawn", FixtureStatus.POSTPONED, True),
            ]
        )
        await db.commit()
        ids = {"sport_id": sport.id, "slug": slug}
    yield ids
    async with async_session_factory() as db:
        await db.execute(delete(Fixture).where(Fixture.sport_id == ids["sport_id"]))
        await db.execute(delete(Team).where(Team.sport_id == ids["sport_id"]))
        await db.execute(delete(League).where(League.sport_id == ids["sport_id"]))
        await db.execute(delete(Sport).where(Sport.id == ids["sport_id"]))
        await db.commit()


async def _external_ids(api_client, slug):
    resp = await api_client.get(f"/fixtures?sport_slug={slug}&limit=50")
    assert resp.status_code == 200
    return [f["id"] for f in resp.json()], resp.json()


@pytest.mark.asyncio
async def test_a_withdrawn_fixture_is_not_listed(world, api_client):
    _, body = await _external_ids(api_client, world["slug"])
    assert len(body) == 2  # the withdrawn one is gone


@pytest.mark.asyncio
async def test_a_genuinely_called_off_fixture_is_still_listed(world, api_client):
    """THE guard. Users explicitly asked to see postponed matches rather than have them vanish;
    hiding by `status == POSTPONED` instead of by `withdrawn` would silently undo that."""
    _, body = await _external_ids(api_client, world["slug"])
    statuses = sorted(f["status"] for f in body)
    assert statuses == ["postponed", "scheduled"]


@pytest.mark.asyncio
async def test_hiding_survives_an_explicit_postponed_status_filter(world, api_client):
    """A caller asking for postponed fixtures should get the real one, not the phantom."""
    resp = await api_client.get(f"/fixtures?sport_slug={world['slug']}&status=postponed&limit=50")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_detail_still_serves_a_withdrawn_fixture(world, api_client):
    """Hidden from the feed, not deleted — an existing deep link or watchlist entry must not
    start 404ing because the provider reshuffled a draw."""
    async with async_session_factory() as db:
        from sqlalchemy import select

        fixture_id = (
            await db.execute(
                select(Fixture.id).where(
                    Fixture.sport_id == world["sport_id"], Fixture.withdrawn.is_(True)
                )
            )
        ).scalar_one()
    resp = await api_client.get(f"/fixtures/{fixture_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "postponed"
