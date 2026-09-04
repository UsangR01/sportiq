"""A sport whose active model has no artefact in the image is dark, and must repair itself.

THE OUTAGE, 2026-09-04. Reported as "for a couple of days now, there's been no predictions in
prod. Looking into the weekend, also no games found." Measured: 1 of 190 upcoming football
fixtures carried a prediction, against tennis at 7 of 7. models_registry had
`football_xgb_v20260819075846` active; the image ships `football_xgb_20260823131011.joblib` and
no Aug 19 file at all. Every football prediction resolved a model and then failed to load it --
silently, because the exception dies inside a Celery task nobody was reading.

"No games found" was the same fault one step downstream: the fixtures were there all along
(60 on the Saturday, 55 on the Sunday) and the feed drops a fixture with no pick.
"""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from app.core.database import async_session_factory
from app.predictions.models import ModelRegistry
from app.sports.models import Sport
from app.workers import reconcile_models as rm


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """An image directory holding ONE servable football artefact plus the blind one.

    Mirrors production: the blind artefact is the newest entry, which is exactly why a naive
    "latest wins" rule would promote a model that has never seen a price.
    """
    (tmp_path / "football_xgb_20260823131011.joblib").write_bytes(b"artefact")
    (tmp_path / "football_xgb_20260822212129_blind.joblib").write_bytes(b"blind")
    manifest = [
        {
            "sport_slug": "football",
            "version": "football_xgb_v20260823131011",
            "artefact_path": "football_xgb_20260823131011.joblib",
            "accuracy": 0.5082,
            "rps_score": 0.2084,
            "roi_simulation": -0.0507,
            "trained_at": "2026-08-23T13:10:16.178049+00:00",
        },
        {
            "sport_slug": "football",
            "version": "football_xgb_v20260822212129_blind",
            "artefact_path": "football_xgb_20260822212129_blind.joblib",
            "accuracy": 0.5021,
            "rps_score": 0.2119,
            "roi_simulation": 0.0701,
            "trained_at": "2026-08-22T21:21:33.654462+00:00",
        },
    ]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    class _Settings:
        models_path = tmp_path

    monkeypatch.setattr(rm, "get_settings", lambda: _Settings())
    monkeypatch.setattr(
        "app.models_ml.base.resolve_artefact_path", lambda stored: str(tmp_path / stored)
    )
    return tmp_path


@pytest.fixture
async def football_sport():
    async with async_session_factory() as db:
        sport = (
            await db.execute(select(Sport).where(Sport.slug == "football"))
        ).scalar_one_or_none()
        created = None
        if sport is None:
            sport = Sport(slug="football", name="Football", model_type="test", active=True)
            db.add(sport)
            await db.flush()
            created = sport.id
        sport_id = sport.id
        # Start from a known state; other suites seed registry rows for this sport.
        await db.execute(delete(ModelRegistry).where(ModelRegistry.sport_id == sport_id))
        await db.commit()

    yield sport_id

    async with async_session_factory() as db:
        await db.execute(delete(ModelRegistry).where(ModelRegistry.sport_id == sport_id))
        if created:
            await db.execute(delete(Sport).where(Sport.id == created))
        await db.commit()


async def _add(sport_id, version, artefact, active):
    async with async_session_factory() as db:
        db.add(
            ModelRegistry(
                sport_id=sport_id,
                version=version,
                artefact_path=artefact,
                accuracy=0.5,
                rps_score=0.21,
                roi_simulation=None,
                trained_at=datetime.now(UTC),
                is_active=active,
            )
        )
        await db.commit()


async def _active(sport_id):
    async with async_session_factory() as db:
        row = (
            await db.execute(
                select(ModelRegistry).where(
                    ModelRegistry.sport_id == sport_id, ModelRegistry.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()
        return row.version if row else None


async def test_the_production_state_repairs_itself(staged, football_sport):
    """THE REGRESSION, rebuilt exactly: the active row names an artefact this image never
    shipped, so the sport cannot predict at all."""
    await _add(
        football_sport, "football_xgb_v20260819075846", "football_xgb_20260819075846.joblib", True
    )

    async with async_session_factory() as db:
        repaired = await rm.reconcile(db)

    assert repaired == ["football"]
    assert await _active(football_sport) == "football_xgb_v20260823131011"


async def test_a_row_that_already_exists_but_is_inactive_is_activated(staged, football_sport):
    """THE CASE seed_model_registry.py CANNOT FIX, and the reason this is a separate function
    rather than a call to that script: its insert-only path sees the version in the table,
    prints "already registered", and leaves the sport dark."""
    await _add(
        football_sport, "football_xgb_v20260819075846", "football_xgb_20260819075846.joblib", True
    )
    await _add(
        football_sport, "football_xgb_v20260823131011", "football_xgb_20260823131011.joblib", False
    )

    async with async_session_factory() as db:
        assert await rm.reconcile(db) == ["football"]

    assert await _active(football_sport) == "football_xgb_v20260823131011"


async def test_a_loadable_model_is_never_touched(staged, football_sport):
    """DELIBERATELY NARROW. A prod-side promotion of an older version is a real decision, and
    this must not quietly override it just because the manifest names something else."""
    await _add(
        football_sport, "football_xgb_v20260823131011", "football_xgb_20260823131011.joblib", True
    )

    async with async_session_factory() as db:
        assert await rm.reconcile(db) == []

    assert await _active(football_sport) == "football_xgb_v20260823131011"


async def test_a_sport_with_no_active_model_at_all_is_repaired(staged, football_sport):
    """The --no-activate failure, which stranded NBA with zero active rows and stopped the sport
    predicting with no error anywhere. Same symptom, same repair."""
    async with async_session_factory() as db:
        assert await rm.reconcile(db) == ["football"]

    assert await _active(football_sport) == "football_xgb_v20260823131011"


async def test_the_market_blind_artefact_is_never_promoted(staged, football_sport):
    """It is the NEWEST entry in the manifest and it has never seen a price -- measurably worse
    (accuracy 0.5021 vs 0.5082) than the model it exists to explain."""
    await _add(
        football_sport, "football_xgb_v20260819075846", "football_xgb_20260819075846.joblib", True
    )

    async with async_session_factory() as db:
        await rm.reconcile(db)

    assert await _active(football_sport) == "football_xgb_v20260823131011"


async def test_nothing_to_fall_back_to_leaves_the_row_alone_and_says_so(
    staged, football_sport, caplog
):
    """When the image ships no servable artefact either, there is no repair to make. The row
    must be left as it is -- deactivating it would turn one broken sport into a sport with no
    model at all -- and it must be LOUD, because that state needs a deploy."""
    (staged / "football_xgb_20260823131011.joblib").unlink()
    await _add(
        football_sport, "football_xgb_v20260819075846", "football_xgb_20260819075846.joblib", True
    )

    with caplog.at_level("ERROR"):
        async with async_session_factory() as db:
            assert await rm.reconcile(db) == []

    assert await _active(football_sport) == "football_xgb_v20260819075846"
    assert any("SPORT DARK" in r.message for r in caplog.records)


async def test_repairing_is_idempotent(staged, football_sport):
    """It runs on every worker start, so a second pass must be a no-op rather than churning the
    registry each time a container restarts."""
    await _add(
        football_sport, "football_xgb_v20260819075846", "football_xgb_20260819075846.joblib", True
    )

    async with async_session_factory() as db:
        assert await rm.reconcile(db) == ["football"]
    async with async_session_factory() as db:
        assert await rm.reconcile(db) == []

    async with async_session_factory() as db:
        actives = (
            await db.execute(
                select(ModelRegistry.version).where(
                    ModelRegistry.sport_id == football_sport,
                    ModelRegistry.is_active.is_(True),
                )
            )
        ).all()
    assert len(actives) == 1, "exactly one active row per sport, always"


def test_the_worker_reconciles_on_every_start():
    """Pinned by source because the behaviour is a signal handler: nothing breaks if it is
    removed, the next drift is simply invisible again for as long as nobody looks."""
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "workers" / "celery.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_reconcile_serving_models"
    )
    decorators = {ast.unparse(d) for d in handler.decorator_list}
    assert "worker_ready.connect" in decorators
    body = ast.unparse(handler)
    assert "reconcile_models" in body
    # Dispatched by NAME, so the reconciler never drags xgboost into the process that queues it.
    assert "send_task" in body
    # A failed reconciliation must never stop the worker booting.
    assert any(isinstance(node, ast.Try) for node in ast.walk(handler))


def test_reconcile_models_is_in_the_workers_include_list():
    """A task the worker does not import is a task it cannot run -- the same latent bug that
    made backfill_predictions raise "Received unregistered task" on a fresh worker."""
    from app.workers.celery import celery_app

    assert "app.workers.reconcile_models" in celery_app.conf.include
