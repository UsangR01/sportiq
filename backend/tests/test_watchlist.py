"""GET/POST/DELETE /user/watchlist (PRD PICK-07, TDD §5.4) — real HTTP round trips against a
real registered user and real seeded fixtures.

Uses httpx.AsyncClient rather than the sync TestClient for the same reason as
tests/test_push_token.py: mixing TestClient's own internal event loop with async DB access in
one file hits the Windows asyncpg/event-loop problem conftest.py guards against.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.main import app
from app.sports.models import League, Sport
from app.users.models import WatchlistItem


@pytest.fixture
async def api_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _register(client: AsyncClient) -> str:
    email = f"watchlist-test-{uuid.uuid4().hex[:8]}@example.com"
    response = await client.post(
        "/auth/register", json={"email": email, "password": "testpassword123"}
    )
    assert response.status_code == 201
    return response.json()["access_token"]


async def _seed_fixture(kickoff: datetime | None = None) -> uuid.UUID:
    """A real Sport/League/Team/Fixture chain, since the endpoint joins across all of them."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_factory() as db:
        sport = Sport(slug=f"wl-sport-{suffix}", name="Watchlist Test Sport", model_type="none")
        db.add(sport)
        await db.flush()
        league = League(sport_id=sport.id, slug=f"wl-league-{suffix}", name="WL League")
        db.add(league)
        await db.flush()
        home = Team(
            sport_id=sport.id, league_id=league.id, name="Home FC", external_id=f"h{suffix}"
        )
        away = Team(
            sport_id=sport.id, league_id=league.id, name="Away FC", external_id=f"a{suffix}"
        )
        db.add_all([home, away])
        await db.flush()
        fixture = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            home_team_id=home.id,
            away_team_id=away.id,
            external_id=f"wl-fixture-{suffix}",
            kickoff_utc=kickoff or datetime.now(UTC) + timedelta(days=1),
            status=FixtureStatus.SCHEDULED,
            season="2026",
        )
        db.add(fixture)
        await db.commit()
        return fixture.id


async def test_add_list_and_remove_round_trip(api_client):
    token = await _register(api_client)
    auth = {"Authorization": f"Bearer {token}"}
    fixture_id = await _seed_fixture()

    assert (await api_client.get("/user/watchlist", headers=auth)).json() == []

    assert (
        await api_client.post("/user/watchlist", json={"fixture_id": str(fixture_id)}, headers=auth)
    ).status_code == 204

    items = (await api_client.get("/user/watchlist", headers=auth)).json()
    assert len(items) == 1
    assert items[0]["fixture_id"] == str(fixture_id)
    assert items[0]["home_team"] == "Home FC"
    assert items[0]["away_team"] == "Away FC"
    assert items[0]["status"] == "scheduled"

    assert (
        await api_client.delete(f"/user/watchlist/{fixture_id}", headers=auth)
    ).status_code == 204
    assert (await api_client.get("/user/watchlist", headers=auth)).json() == []


async def test_saving_twice_does_not_create_a_second_row(api_client):
    """Idempotent by design: a double tap on a flaky connection must not produce two rows, and
    therefore not two kickoff reminders."""
    token = await _register(api_client)
    auth = {"Authorization": f"Bearer {token}"}
    fixture_id = await _seed_fixture()

    for _ in range(2):
        response = await api_client.post(
            "/user/watchlist", json={"fixture_id": str(fixture_id)}, headers=auth
        )
        assert response.status_code == 204

    assert len((await api_client.get("/user/watchlist", headers=auth)).json()) == 1


async def test_removing_something_not_saved_is_not_an_error(api_client):
    """204 rather than 404, so the client never has to reconcile which state it thought it was
    in — the end state is the same either way."""
    token = await _register(api_client)
    auth = {"Authorization": f"Bearer {token}"}
    response = await api_client.delete(f"/user/watchlist/{uuid.uuid4()}", headers=auth)
    assert response.status_code == 204


async def test_saving_an_unknown_fixture_is_404(api_client):
    token = await _register(api_client)
    response = await api_client.post(
        "/user/watchlist",
        json={"fixture_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


async def test_requires_auth(api_client):
    """A watchlist is durable, per-account state that drives a push notification, so unlike the
    browsing endpoints it is deliberately not available to guests."""
    assert (await api_client.get("/user/watchlist")).status_code == 401
    assert (
        await api_client.post("/user/watchlist", json={"fixture_id": str(uuid.uuid4())})
    ).status_code == 401
    assert (await api_client.delete(f"/user/watchlist/{uuid.uuid4()}")).status_code == 401


async def test_one_users_watchlist_is_not_visible_to_another(api_client):
    """The list is filtered by user_id, not just by fixture — worth asserting explicitly since
    a missing where-clause here leaks what other people are following."""
    token_a = await _register(api_client)
    token_b = await _register(api_client)
    fixture_id = await _seed_fixture()

    await api_client.post(
        "/user/watchlist",
        json={"fixture_id": str(fixture_id)},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    other = await api_client.get("/user/watchlist", headers={"Authorization": f"Bearer {token_b}"})
    assert other.json() == []


async def test_listed_in_kickoff_order(api_client):
    """A watchlist answers "what am I waiting on", so the next match to start comes first —
    not the most recently saved."""
    token = await _register(api_client)
    auth = {"Authorization": f"Bearer {token}"}
    now = datetime.now(UTC)
    later = await _seed_fixture(now + timedelta(days=3))
    sooner = await _seed_fixture(now + timedelta(days=1))

    # Saved in the WRONG order on purpose, so insertion order cannot accidentally satisfy this.
    for fixture_id in (later, sooner):
        await api_client.post("/user/watchlist", json={"fixture_id": str(fixture_id)}, headers=auth)

    items = (await api_client.get("/user/watchlist", headers=auth)).json()
    assert [i["fixture_id"] for i in items] == [str(sooner), str(later)]


async def test_deleting_a_fixture_removes_its_watchlist_rows(api_client):
    """ON DELETE CASCADE — a watchlist row is meaningless once its fixture is gone, and the
    tennis purge showed what happens when dependents are left to be cleaned up by hand."""
    token = await _register(api_client)
    auth = {"Authorization": f"Bearer {token}"}
    fixture_id = await _seed_fixture()
    await api_client.post("/user/watchlist", json={"fixture_id": str(fixture_id)}, headers=auth)

    async with async_session_factory() as db:
        await db.execute(delete(Fixture).where(Fixture.id == fixture_id))
        await db.commit()
        remaining = (
            await db.execute(select(WatchlistItem).where(WatchlistItem.fixture_id == fixture_id))
        ).all()
    assert remaining == []
