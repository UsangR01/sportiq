"""The serving registry and the artefacts in the image must agree, or a sport goes dark.

DELIBERATELY FREE OF CELERY. Both the worker and the API run this at startup, and importing
app.workers.celery would pull celery and kombu into the API process -- which runs two gunicorn
workers on a 512MB instance and has already been OOM-killed once by an import it did not need.
The task wrapper and the catch-up queueing live in app/workers/reconcile_models.py.

WHAT HAPPENED, 2026-09-04. Football produced no predictions for days -- 1 of 190 upcoming
fixtures had one, while tennis was 7 of 7 -- and nothing about it was visible in the product.
models_registry had `football_xgb_v20260819075846` active; the image ships
`football_xgb_20260823131011.joblib` and no Aug 19 file at all, because that artefact was never
staged. So every football prediction resolved a model and then failed to load it.

THE TWO HALVES DRIFT INDEPENDENTLY, which is the whole hazard:

    ml/artifacts/deployed/   ships with the image, changes on deploy
    models_registry          a DB row, changes when a training run or a script says so

Neither knows about the other. CLAUDE.md already records "promotion is a DB update, not a
redeploy" as a FEATURE, and this is its cost: a promotion that never ships its file, and a
deploy that ships a file nobody activated, both end in a sport that silently stops predicting.
It is the third appearance of this shape -- after --no-activate stranded NBA with no active row
at all, and after a retrain reached nobody because the queue guard only asked "does a prediction
exist".

WHY REPAIR RATHER THAN ONLY ALERT. The failure is total for the affected sport and the recovery
is unambiguous: the image contains exactly one servable artefact per sport, so a row pointing
anywhere else cannot be served by this container whatever anyone intended.

DELIBERATELY NARROW. It acts only when the active row's artefact is ABSENT, never when a
perfectly loadable model merely differs from the manifest -- a deliberate prod-side promotion of
an older version stays exactly as chosen. And it never activates a market-blind artefact, which
exists to explain picks and is measurably worse than the model it explains.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.predictions.explanation import BLIND_VERSION_SUFFIX
from app.predictions.models import ModelRegistry
from app.sports.models import Sport

logger = logging.getLogger(__name__)

REPO_DEPLOYED = Path(__file__).resolve().parents[2] / "ml" / "artifacts" / "deployed"


def _manifest_entries() -> list[dict]:
    """The manifest travels WITH the artefacts, so it is found the same way they are."""
    models_dir = get_settings().models_path
    candidate = models_dir / "manifest.json"
    path = candidate if candidate.is_file() else REPO_DEPLOYED / "manifest.json"
    if not path.is_file():
        logger.warning("no model manifest at %s - cannot reconcile the registry", path)
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _artefact_exists(artefact_path: str) -> bool:
    from app.models_ml.base import resolve_artefact_path

    return Path(resolve_artefact_path(artefact_path)).is_file()


async def reconcile(db: AsyncSession) -> list[str]:
    """Repairs any sport whose active model cannot be loaded. Returns the slugs repaired."""
    entries = _manifest_entries()
    if not entries:
        return []
    # The blind artefact is the NEWEST football entry, so a naive "latest wins" rule would
    # promote it. Excluded here for the same reason seed_model_registry.py excludes it.
    servable = {
        e["sport_slug"]: e
        for e in entries
        if not e["version"].endswith(BLIND_VERSION_SUFFIX) and _artefact_exists(e["artefact_path"])
    }
    sports = {slug: sid for sid, slug in (await db.execute(select(Sport.id, Sport.slug))).all()}

    repaired: list[str] = []
    for slug, sport_id in sports.items():
        active = (
            await db.execute(
                select(ModelRegistry).where(
                    ModelRegistry.sport_id == sport_id, ModelRegistry.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()
        if active is not None and _artefact_exists(active.artefact_path):
            continue  # loadable - leave whatever was deliberately chosen alone

        entry = servable.get(slug)
        if entry is None:
            # Nothing to fall back to. Loud, because the sport is dark either way and the only
            # fix is a deploy that ships an artefact.
            if active is not None:
                logger.error(
                    "SPORT DARK - %s serves %s whose artefact %s is not in this image, and the "
                    "manifest offers no replacement. Every prediction for it fails.",
                    slug,
                    active.version,
                    active.artefact_path,
                )
            continue

        reason = "no active model at all" if active is None else f"{active.version} has no artefact"
        row = (
            await db.execute(select(ModelRegistry).where(ModelRegistry.version == entry["version"]))
        ).scalar_one_or_none()
        if row is None:
            db.add(
                ModelRegistry(
                    sport_id=sport_id,
                    version=entry["version"],
                    artefact_path=entry["artefact_path"],
                    accuracy=entry["accuracy"],
                    rps_score=entry["rps_score"],
                    roi_simulation=entry["roi_simulation"],
                    trained_at=datetime.fromisoformat(entry["trained_at"]),
                    is_active=True,
                )
            )
        else:
            # THE CASE seed_model_registry.py CANNOT FIX: the row already exists and is merely
            # inactive, so its insert-only path skips it as "already registered" and the sport
            # stays dark. Activation has to be idempotent, not a side effect of inserting.
            row.is_active = True
        if active is not None:
            active.is_active = False
        logger.error(
            "REPAIRED SERVING MODEL - %s was dark (%s); activating %s, whose artefact ships in "
            "this image. Predictions for this sport had been failing until now.",
            slug,
            reason,
            entry["version"],
        )
        repaired.append(slug)

    if repaired:
        await db.commit()
    return repaired
