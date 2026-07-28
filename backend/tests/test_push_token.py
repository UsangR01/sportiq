"""PUT /user/push-token (TDD §5.4) — real HTTP round trip via a real registered user, plus a
unit test of _send_push's self-healing behaviour when Expo reports a token as dead.

Uses httpx.AsyncClient (not the sync TestClient) throughout so every test shares pytest-asyncio's
one event loop per test — mixing the sync TestClient's own internal loop with async DB access in
the same file hits the exact Windows asyncpg/event-loop issue tests/conftest.py's engine-dispose
fixture guards against (see that file's docstring)."""

import uuid
from types import SimpleNamespace

import jwt
import pytest
from exponent_server_sdk import DeviceNotRegisteredError
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.database import async_session_factory
from app.main import app
from app.users.models import User


@pytest.fixture
async def api_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _register(client: AsyncClient) -> str:
    email = f"push-test-{uuid.uuid4().hex[:8]}@example.com"
    response = await client.post(
        "/auth/register", json={"email": email, "password": "testpassword123"}
    )
    assert response.status_code == 201
    return response.json()["access_token"]


async def test_rejects_a_non_expo_token(api_client):
    token = await _register(api_client)
    response = await api_client.put(
        "/user/push-token",
        json={"expo_push_token": "not-a-real-token"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


async def test_accepts_and_persists_a_valid_expo_token(api_client):
    token = await _register(api_client)
    expo_token = "ExponentPushToken[abcDEF123]"
    response = await api_client.put(
        "/user/push-token",
        json={"expo_push_token": expo_token},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 204

    async with async_session_factory() as db:
        payload = jwt.decode(token, options={"verify_signature": False})
        user = (await db.execute(select(User).where(User.id == payload["sub"]))).scalar_one()
        assert user.expo_push_token == expo_token


async def test_null_token_clears_it(api_client):
    token = await _register(api_client)
    await api_client.put(
        "/user/push-token",
        json={"expo_push_token": "ExponentPushToken[abcDEF123]"},
        headers={"Authorization": f"Bearer {token}"},
    )
    response = await api_client.put(
        "/user/push-token",
        json={"expo_push_token": None},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 204

    async with async_session_factory() as db:
        payload = jwt.decode(token, options={"verify_signature": False})
        user = (await db.execute(select(User).where(User.id == payload["sub"]))).scalar_one()
        assert user.expo_push_token is None


async def test_requires_auth(api_client):
    response = await api_client.put(
        "/user/push-token", json={"expo_push_token": "ExponentPushToken[x]"}
    )
    assert response.status_code == 401


async def test_send_push_clears_a_dead_token_on_device_not_registered(monkeypatch):
    from app.workers import notify_users

    async with async_session_factory() as db:
        user = User(email=f"dead-token-{uuid.uuid4().hex[:8]}@example.com", password_hash="x")
        user.expo_push_token = "ExponentPushToken[dead]"
        db.add(user)
        await db.commit()
        await db.refresh(user)

        class _FakeClient:
            def publish(self, message):
                raise DeviceNotRegisteredError(SimpleNamespace(message="dead"))

        monkeypatch.setattr(notify_users, "_push_client", lambda: _FakeClient())

        await notify_users._send_push(db, user, title="t", body="b", data={})

        await db.refresh(user)
        assert user.expo_push_token is None
