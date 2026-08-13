"""Register the staged model artefacts on a database that has never seen a training run.

WHY THIS EXISTS. A fresh deployment has tables (alembic) and sports (seed_sports.py) and still
serves NOTHING: models_registry is empty, so app/models_ml/runner.py resolves no model for any
sport and every prediction silently fails to be produced. The artefacts are sitting in the image
the whole time, correctly staged, with no row pointing at them.

That failure is invisible from outside. Nothing raises at boot, ingest keeps writing fixtures,
and the feed just never gains a pick -- the same shape as the day --no-activate demoted NBA's
only model and the sport stopped predicting with no error anywhere.

Registration normally happens as a side effect of training, which only ever runs on a machine
with the training data. Nothing carried that across to a deployed database until this script.

Reads ml/artifacts/deployed/manifest.json, written by stage_artefacts.py so the two cannot drift
-- you cannot stage an artefact without recording the version and metrics it shipped with.

IDEMPOTENT, and safe to re-run after a redeploy: a version already present is left untouched
rather than duplicated, and activation is only ever moved when this script is genuinely
introducing the newer artefact for that sport.

    PYTHONPATH=. python scripts/seed_model_registry.py            # dry run
    PYTHONPATH=. python scripts/seed_model_registry.py --confirm
"""

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import async_session_factory, engine
from app.predictions.models import ModelRegistry
from app.sports.models import Sport

# The manifest travels WITH the artefacts, so it is found the same way they are: MODELS_DIR in a
# container, the repo's own ml/artifacts/deployed locally.
REPO_DEPLOYED = Path(__file__).resolve().parents[2] / "ml" / "artifacts" / "deployed"


def _manifest_path() -> Path:
    models_dir = get_settings().models_path
    candidate = models_dir / "manifest.json"
    return candidate if candidate.is_file() else REPO_DEPLOYED / "manifest.json"


async def main(confirm: bool) -> None:
    manifest_path = _manifest_path()
    if not manifest_path.is_file():
        print(f"no manifest at {manifest_path} — run stage_artefacts.py --confirm first")
        await engine.dispose()
        raise SystemExit(1)

    entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    print(f"manifest: {manifest_path}  ({len(entries)} models)\n")

    async with async_session_factory() as db:
        sports = {
            slug: sport_id
            for sport_id, slug in (await db.execute(select(Sport.id, Sport.slug))).all()
        }
        existing = {
            version for (version,) in (await db.execute(select(ModelRegistry.version))).all()
        }

        planned = []
        for entry in entries:
            sport_slug = entry["sport_slug"]
            artefact = get_settings().models_path / entry["artefact_path"]
            if sport_slug not in sports:
                # Ordering matters and the message should say so: seed_sports.py first.
                print(f"  {sport_slug:<9} NO SPORT ROW — run seed_sports.py first")
                continue
            if entry["version"] in existing:
                print(f"  {sport_slug:<9} already registered  {entry['version']}")
                continue
            if not artefact.is_file():
                # Registering a row whose file is absent produces a model that resolves and
                # then fails to load — strictly worse than no row, which at least fails loudly
                # at the same point every time.
                print(f"  {sport_slug:<9} ARTEFACT MISSING    {entry['artefact_path']}")
                continue
            print(f"  {sport_slug:<9} will register       {entry['version']}")
            planned.append(entry)

        if not planned:
            print("\nnothing to do")
            await engine.dispose()
            return
        if not confirm:
            print("\ndry run — re-run with --confirm to write")
            await engine.dispose()
            return

        for entry in planned:
            sport_id = sports[entry["sport_slug"]]
            # Demote this sport's current active row only because a newer artefact is genuinely
            # replacing it. Blanket demotion is what once left NBA with no active model at all.
            for row in (
                (
                    await db.execute(
                        select(ModelRegistry).where(
                            ModelRegistry.sport_id == sport_id,
                            ModelRegistry.is_active.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            ):
                row.is_active = False
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
            print(f"  registered {entry['version']} (is_active=True)")
        await db.commit()

        # Every sport must end with exactly one active model, or it stops predicting.
        for slug, sport_id in sorted(sports.items()):
            active = (
                (
                    await db.execute(
                        select(ModelRegistry.version).where(
                            ModelRegistry.sport_id == sport_id,
                            ModelRegistry.is_active.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            state = active[0] if len(active) == 1 else f"{len(active)} ACTIVE — WRONG"
            print(f"  {slug:<9} {state}")

    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="actually write the rows")
    asyncio.run(main(parser.parse_args().confirm))
