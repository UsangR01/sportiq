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
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team, TeamFeatures
from app.history.models import Outcome
from app.main import app
from app.predictions.models import Prediction
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


@pytest.fixture(autouse=True)
async def _clean_up_seeded_sports():
    """Delete every Sport this module seeded, after each test.

    Originally written because the suite ran against the DEV database, where anything left
    behind showed up in the running app — the sport dropdown filled with "Watchlist Test Sport"
    entries, 24 of them across several runs. That hazard is gone: the suite now uses a
    dedicated database (see conftest.py). Kept because per-test cleanup is worth having on its
    own — it keeps each test independent of what its neighbours left, which conftest's
    once-per-session truncation cannot do.

    Keyed on the wl-sport- slug prefix rather than ids captured during the test, so a test that
    fails part-way through still cleans up.

    team_features is in the list because a seeded fixture does not stay inert: the running
    ingest worker picked these up and computed feature vectors for them, and 30 such rows had
    to be removed by hand. Only fixture -> watchlist_items cascades; every other child needs
    deleting explicitly, and a missing one fails the whole delete on a FK violation.
    """
    yield
    async with async_session_factory() as db:
        sport_ids = (
            (await db.execute(select(Sport.id).where(Sport.slug.like("wl-sport-%"))))
            .scalars()
            .all()
        )
        if not sport_ids:
            return
        fixture_ids = (
            (await db.execute(select(Fixture.id).where(Fixture.sport_id.in_(sport_ids))))
            .scalars()
            .all()
        )
        if fixture_ids:
            for model in (TeamFeatures, Prediction, Outcome, FixtureLiveState, WatchlistItem):
                await db.execute(delete(model).where(model.fixture_id.in_(fixture_ids)))
        await db.execute(delete(Fixture).where(Fixture.sport_id.in_(sport_ids)))
        await db.execute(delete(Team).where(Team.sport_id.in_(sport_ids)))
        await db.execute(delete(League).where(League.sport_id.in_(sport_ids)))
        await db.execute(delete(Sport).where(Sport.id.in_(sport_ids)))
        await db.commit()


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


async def test_saving_a_fixture_arms_its_reminder_immediately(api_client):
    """Saving is when a user expects the reminder to be set.

    The daily sweep runs at 02:00 UTC, so relying on it alone means a fixture saved during the
    day for a match later that day or the next morning is never queued and the reminder simply
    never arrives - silently, with nothing in any log to suggest it should have.
    """
    from unittest.mock import patch

    from app.workers.ingest_fixtures import KICKOFF_REMINDER_MINUTES

    kickoff = datetime.now(UTC) + timedelta(hours=10)
    fixture_id = await _seed_fixture(kickoff)
    token = await _register(api_client)

    with patch("app.workers.notify_users.notify_kickoff_reminder.apply_async") as queued:
        response = await api_client.post(
            "/user/watchlist",
            json={"fixture_id": str(fixture_id)},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 204
    assert queued.call_count == 1
    eta = queued.call_args.kwargs["eta"]
    expected = kickoff - timedelta(minutes=KICKOFF_REMINDER_MINUTES)
    assert abs((eta - expected).total_seconds()) < 2


async def test_saving_does_not_arm_a_reminder_for_an_estimated_kickoff(api_client):
    """An estimated kickoff is a DATE, not a time (common for tennis). "Starts in an hour" off
    a midnight placeholder is a false claim, so no reminder is armed at all."""
    from unittest.mock import patch

    fixture_id = await _seed_fixture(datetime.now(UTC) + timedelta(hours=10))
    async with async_session_factory() as db:
        fixture = (await db.execute(select(Fixture).where(Fixture.id == fixture_id))).scalar_one()
        fixture.kickoff_is_estimated = True
        await db.commit()
    token = await _register(api_client)

    with patch("app.workers.notify_users.notify_kickoff_reminder.apply_async") as queued:
        response = await api_client.post(
            "/user/watchlist",
            json={"fixture_id": str(fixture_id)},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 204
    assert queued.call_count == 0


async def test_a_broker_failure_does_not_lose_the_save(api_client):
    """The fixture must still be saved if the queue is unreachable. The daily sweep is the
    backstop, so degrading to "saved but not yet armed" is right; losing the save is not."""
    from unittest.mock import patch

    fixture_id = await _seed_fixture(datetime.now(UTC) + timedelta(hours=10))
    token = await _register(api_client)
    auth = {"Authorization": f"Bearer {token}"}

    with patch(
        "app.workers.notify_users.notify_kickoff_reminder.apply_async",
        side_effect=OSError("broker down"),
    ):
        response = await api_client.post(
            "/user/watchlist", json={"fixture_id": str(fixture_id)}, headers=auth
        )

    assert response.status_code == 204
    listed = (await api_client.get("/user/watchlist", headers=auth)).json()
    assert any(item["fixture_id"] == str(fixture_id) for item in listed)


# === The saved pick: a receipt, frozen at the moment the user acted ==========================


async def _seed_prediction(fixture_id: uuid.UUID, home_prob: float) -> None:
    """A real Prediction row so the endpoint has a pick to capture."""
    from app.predictions.models import ConfidenceTier, PredictionKind

    async with async_session_factory() as db:
        db.add(
            Prediction(
                fixture_id=fixture_id,
                model_version="watchlist-test-v1",
                home_prob=home_prob,
                draw_prob=None,
                away_prob=1 - home_prob,
                confidence_tier=ConfidenceTier.HIGH,
                kind=PredictionKind.PRE_MATCH,
            )
        )
        await db.commit()


async def test_saving_records_the_pick_that_was_shown(api_client):
    """The point of the whole feature: best_pick is recomputed per request and never stored, so
    without this a user who saved "HOME 88%" can open their watchlist and be shown something
    else entirely — reported as the app changing its mind after they acted."""
    token = await _register(api_client)
    auth = {"Authorization": f"Bearer {token}"}
    fixture_id = await _seed_fixture()
    await _seed_prediction(fixture_id, home_prob=0.88)

    await api_client.post("/user/watchlist", json={"fixture_id": str(fixture_id)}, headers=auth)
    item = (await api_client.get("/user/watchlist", headers=auth)).json()[0]

    assert item["saved_market"] == "h2h"
    assert item["saved_selection"] == "home"
    assert item["saved_probability"] == pytest.approx(0.88)


async def test_the_saved_pick_does_not_move_when_the_model_changes_its_mind(api_client):
    """THE LOAD-BEARING TEST. A later prediction must not rewrite what the user was shown —
    the feed is free to update, the receipt is not."""
    token = await _register(api_client)
    auth = {"Authorization": f"Bearer {token}"}
    fixture_id = await _seed_fixture()
    await _seed_prediction(fixture_id, home_prob=0.88)

    await api_client.post("/user/watchlist", json={"fixture_id": str(fixture_id)}, headers=auth)

    # The model is re-run and now says the other way round — exactly the overnight swing that
    # prompted this. The live feed should reflect it; the saved row must not.
    await _seed_prediction(fixture_id, home_prob=0.20)

    item = (await api_client.get("/user/watchlist", headers=auth)).json()[0]
    assert item["saved_selection"] == "home"
    assert item["saved_probability"] == pytest.approx(0.88)


async def test_a_fixture_with_no_pick_still_saves(api_client):
    """A fixture whose pick fails the guards has nothing to record, and saving it must still
    work — the user asked to keep the FIXTURE. Null means "no pick was shown", never a
    fabricated one."""
    token = await _register(api_client)
    auth = {"Authorization": f"Bearer {token}"}
    fixture_id = await _seed_fixture()  # no prediction seeded at all

    response = await api_client.post(
        "/user/watchlist", json={"fixture_id": str(fixture_id)}, headers=auth
    )
    assert response.status_code == 204
    item = (await api_client.get("/user/watchlist", headers=auth)).json()[0]
    assert item["saved_market"] is None
    assert item["saved_probability"] is None
