"""Promotion takes effect on the next prediction, not the next restart.

Measured failure 2026-08-18: a worker cached a registry resolution made while the registry
was briefly wrong (pre-seed_model_registry) and then failed every football prediction with
FileNotFoundError until manually restarted — long after the registry itself was corrected.
The cache is now keyed by the ACTIVE VERSION, so flipping is_active reaches a live process
on its next call.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from app.core.database import async_session_factory
from app.models_ml.runner import ModelRunner
from app.predictions.models import ModelRegistry
from app.sports.models import Sport


@pytest.mark.asyncio
async def test_activation_flip_reaches_a_live_runner_without_restart():
    runner = ModelRunner()
    async with async_session_factory() as db:
        sport = Sport(
            id=uuid.uuid4(), slug=f"testsport-{uuid.uuid4().hex[:6]}", name="Test", model_type="xgb"
        )
        db.add(sport)
        await db.commit()
        rows = [
            ModelRegistry(
                id=uuid.uuid4(),
                sport_id=sport.id,
                version=f"nba_xgb_v{i}",
                artefact_path=f"artefact_{i}.joblib",
                is_active=(i == 1),
                trained_at=datetime.now(UTC),
            )
            for i in (1, 2)
        ]
        db.add_all(rows)
        await db.commit()
        sport_id = sport.id

        # The sport slug has no model class registered, so resolution itself must fail AFTER
        # the registry row is found — but we only need version-tracking, so monkey-class it in.
        from app.models_ml import runner as runner_module

        class FakeModel:
            def __init__(self, artefact_path, version):
                self.artefact_path = artefact_path
                self.version = version

        runner_module._MODEL_CLASSES[sport.slug] = FakeModel
        try:
            first = await runner.get_model(db, sport_id)
            assert first.version == "nba_xgb_v1"
            # Same active version: the cached instance is reused, not rebuilt.
            assert await runner.get_model(db, sport_id) is first

            # Flip activation the way scripts/activate_model.py does.
            for row in (
                await db.execute(select(ModelRegistry).where(ModelRegistry.sport_id == sport_id))
            ).scalars():
                row.is_active = row.version == "nba_xgb_v2"
            await db.commit()

            second = await runner.get_model(db, sport_id)
            assert (
                second.version == "nba_xgb_v2"
            ), "a live process must pick up a promotion without restarting"
        finally:
            runner_module._MODEL_CLASSES.pop(sport.slug, None)
            async with async_session_factory() as cleanup:
                await cleanup.execute(
                    delete(ModelRegistry).where(ModelRegistry.sport_id == sport_id)
                )
                await cleanup.execute(delete(Sport).where(Sport.id == sport_id))
                await cleanup.commit()
