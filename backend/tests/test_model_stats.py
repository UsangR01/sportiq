"""GET /stats/model (TDD §4.1) — returns the currently active models_registry row per sport,
joined to the sport's slug. Seeds real Sport/League/ModelRegistry rows rather than mocking,
matching this project's DB-touching test convention (see test_team_upsert.py)."""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.database import async_session_factory
from app.main import app
from app.predictions.models import ModelRegistry
from app.sports.models import League, Sport


@pytest.fixture
async def api_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def seeded_sport_with_models():
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

        now = datetime.now(UTC)
        inactive = ModelRegistry(
            sport_id=sport.id,
            version="test_model_v1",
            artefact_path="/tmp/v1.joblib",
            accuracy=0.60,
            rps_score=0.25,
            roi_simulation=None,
            trained_at=now,
            is_active=False,
        )
        active = ModelRegistry(
            sport_id=sport.id,
            version="test_model_v2",
            artefact_path="/tmp/v2.joblib",
            accuracy=0.65,
            rps_score=0.20,
            roi_simulation=0.15,
            trained_at=now,
            is_active=True,
        )
        db.add_all([inactive, active])
        await db.commit()

    yield slug

    async with async_session_factory() as db:
        await db.execute(delete(ModelRegistry).where(ModelRegistry.sport_id == sport.id))
        await db.execute(delete(League).where(League.sport_id == sport.id))
        await db.execute(delete(Sport).where(Sport.id == sport.id))
        await db.commit()


async def test_returns_only_the_active_model(api_client, seeded_sport_with_models):
    slug = seeded_sport_with_models
    response = await api_client.get("/stats/model", params={"sport_slug": slug})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["model_version"] == "test_model_v2"
    assert body[0]["accuracy"] == 0.65
    assert body[0]["rps_score"] == 0.20
    assert body[0]["roi_simulation"] == 0.15
    assert body[0]["sport_slug"] == slug


async def test_no_sport_filter_still_includes_seeded_sport(api_client, seeded_sport_with_models):
    response = await api_client.get("/stats/model")
    assert response.status_code == 200
    versions = [row["model_version"] for row in response.json()]
    assert "test_model_v2" in versions
    assert "test_model_v1" not in versions


async def test_unknown_sport_slug_returns_empty_list(api_client):
    response = await api_client.get("/stats/model", params={"sport_slug": "no-such-sport"})
    assert response.status_code == 200
    assert response.json() == []
