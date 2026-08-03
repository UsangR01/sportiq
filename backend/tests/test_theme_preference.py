"""user_preferences.theme_preference — the account-synced appearance setting.

Covers the two things easiest to get wrong here: the enum stores uppercase MEMBER NAMES in
Postgres while the API speaks lowercase values (this schema's existing convention — writing
a lowercase name raises InvalidTextRepresentationError), and a partial PUT must not reset
fields it didn't mention.

Registers per test rather than logging in, matching tests/test_push_token.py: /auth/register
already returns an access token, and /auth/login is rate-limited to 5/minute, which a file of
this size exceeds on its own.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def api_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _auth_headers(client: AsyncClient) -> dict[str, str]:
    email = f"theme-test-{uuid.uuid4().hex[:8]}@example.com"
    response = await client.post(
        "/auth/register", json={"email": email, "password": "testpassword123"}
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_defaults_to_system_before_anything_is_saved(api_client):
    """ "System" is the honest default: a user who has never chosen still wants the OS setting,
    which is a real value rather than a null."""
    headers = await _auth_headers(api_client)
    body = (await api_client.get("/user/preferences", headers=headers)).json()
    assert body["theme_preference"] == "system"


@pytest.mark.parametrize("choice", ["light", "dark", "system"])
async def test_round_trips_every_choice(api_client, choice):
    """Guards the uppercase-name/lowercase-value split: the API speaks lowercase both ways
    while Postgres stores LIGHT/DARK/SYSTEM."""
    headers = await _auth_headers(api_client)
    put = await api_client.put(
        "/user/preferences", json={"theme_preference": choice}, headers=headers
    )
    assert put.status_code == 200
    assert put.json()["theme_preference"] == choice

    got = await api_client.get("/user/preferences", headers=headers)
    assert got.json()["theme_preference"] == choice


async def test_rejects_an_unknown_theme(api_client):
    headers = await _auth_headers(api_client)
    response = await api_client.put(
        "/user/preferences", json={"theme_preference": "midnight"}, headers=headers
    )
    assert response.status_code == 422


async def test_updating_another_field_leaves_theme_untouched(api_client):
    """A partial PUT must not silently reset the theme — the mobile client sends only the
    field it is actually changing."""
    headers = await _auth_headers(api_client)
    await api_client.put("/user/preferences", json={"theme_preference": "dark"}, headers=headers)
    await api_client.put("/user/preferences", json={"default_min_odds": 2.5}, headers=headers)

    body = (await api_client.get("/user/preferences", headers=headers)).json()
    assert body["theme_preference"] == "dark"
    assert body["default_min_odds"] == 2.5


async def test_requires_authentication(api_client):
    """Theme is account state, so it sits behind the same gate as the rest of preferences —
    guests keep theirs on-device."""
    assert (await api_client.get("/user/preferences")).status_code in (401, 403)
