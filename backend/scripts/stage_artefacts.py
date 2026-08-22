"""Copy the artefacts models_registry marks ACTIVE into ml/artifacts/deployed/.

WHY THIS EXISTS

Portable artefact paths (see app/models_ml/base.py:resolve_artefact_path) fixed half the
deploy problem: a registry row now names a file rather than a path on the trainer's laptop.
The other half is that the file has to actually be in the image. ml/artifacts/ is gitignored —
correctly, it holds every training run ever (27MB, mostly superseded) — so a container built
from a clean clone would have resolved a perfectly portable filename to nothing at all.

This stages only what is currently serving: ~2.6MB across three sports, tracked in git and
copied into the image by the Dockerfile.

THE TRADEOFF, STATED PLAINLY

TDD §3.1 wants model promotion to be a DB update rather than a redeploy. With artefacts baked
into the image that holds only for a model already shipped in it — promoting a BRAND NEW model
needs this script, a commit, and a deploy. That is a real deviation, accepted because the
alternative (object storage plus a download-on-miss cache) is materially more moving parts than
an MVP deploy needs, and because Render's persistent disks cannot be shared across the web,
worker and beat services, so a disk would mean three copies to keep in sync.

Revisit if promotion frequency ever makes the redeploy painful; the resolver already supports
it, since MODELS_DIR can point anywhere a download step has populated.

Usage (from backend/):
    PYTHONPATH=. python scripts/stage_artefacts.py            # dry run
    PYTHONPATH=. python scripts/stage_artefacts.py --confirm
"""

import argparse
import asyncio
import json
import shutil
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import async_session_factory, engine
from app.predictions.explanation import BLIND_VERSION_SUFFIX
from app.predictions.models import ModelRegistry
from app.sports.models import Sport

DEPLOYED_DIR = Path(__file__).resolve().parents[2] / "ml" / "artifacts" / "deployed"

# What was staged, and what the registry said about it. A FRESH DATABASE HAS NO
# models_registry ROWS AT ALL -- alembic creates the table and nothing fills it -- so an
# image full of artefacts still resolves no model and produces zero predictions, silently.
# scripts/seed_model_registry.py replays this file to close that gap.
#
# Written HERE rather than maintained by hand so the two can never disagree: you cannot
# stage a new artefact without recording which version and metrics it shipped with.
MANIFEST = DEPLOYED_DIR / "manifest.json"


async def _newest_blind_rows(db) -> list[tuple[ModelRegistry, str]]:
    """The most recent market-blind artefact for each sport that has one.

    Newest-per-sport rather than all of them: every measurement run leaves a row, and staging the
    whole history would grow the image without bound for no benefit.
    """
    newest: dict[str, tuple[ModelRegistry, str]] = {}
    rows = (
        await db.execute(
            select(ModelRegistry, Sport.slug)
            .join(Sport, Sport.id == ModelRegistry.sport_id)
            .where(ModelRegistry.version.endswith(BLIND_VERSION_SUFFIX))
            .order_by(ModelRegistry.trained_at.desc())
        )
    ).all()
    for registry_row, sport_slug in rows:
        newest.setdefault(sport_slug, (registry_row, sport_slug))
    return list(newest.values())


async def main(confirm: bool) -> None:
    source_dir = get_settings().models_path
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(ModelRegistry, Sport.slug)
                .join(Sport, Sport.id == ModelRegistry.sport_id)
                .where(ModelRegistry.is_active.is_(True))
            )
        ).all()
        # Plus the newest market-blind variant per sport. These are is_active=False BY DESIGN --
        # they must never serve a probability -- so the active-only query above cannot see them,
        # and the stale-clearing step below would DELETE one copied in by hand. Without this the
        # explanation engine silently returns nothing in production while working locally.
        rows += await _newest_blind_rows(db)

    if not rows:
        print("no active models registered — nothing to stage")
        await engine.dispose()
        return

    print(f"source: {source_dir}\ntarget: {DEPLOYED_DIR}\n")
    planned: list[tuple[Path, Path]] = []
    missing = False
    for registry_row, sport_slug in rows:
        # Tolerate a legacy absolute path here: this script may run before the migration on a
        # database restored from an older dump.
        name = registry_row.artefact_path.replace("\\", "/").rsplit("/", 1)[-1]
        source = source_dir / name
        if not source.is_file():
            print(f"  {sport_slug:<9} MISSING  {name}")
            missing = True
            continue
        size_mb = source.stat().st_size / 1048576
        print(f"  {sport_slug:<9} {size_mb:>6.2f} MB  {name}  ({registry_row.version})")
        planned.append((source, DEPLOYED_DIR / name))

    if missing:
        # Staging a partial set would produce an image that starts fine and then fails on the
        # first prediction for whichever sport is absent — a worse failure than stopping here.
        print("\nrefusing to stage: an active model's artefact is missing from the source dir")
        await engine.dispose()
        raise SystemExit(1)

    total = sum(s.stat().st_size for s, _ in planned) / 1048576
    print(f"\n{len(planned)} artefacts, {total:.2f} MB total")
    if not confirm:
        print("dry run — re-run with --confirm to copy")
        await engine.dispose()
        return

    DEPLOYED_DIR.mkdir(parents=True, exist_ok=True)
    # Clear first so a demoted model's artefact does not linger in the image and quietly bloat
    # it, or worse, get loaded by a stale registry row on a rolled-back database.
    for stale in DEPLOYED_DIR.glob("*.joblib"):
        if stale not in {target for _, target in planned}:
            stale.unlink()
            print(f"  removed stale {stale.name}")
    for source, target in planned:
        shutil.copy2(source, target)
        print(f"  staged {target.name}")

    manifest = [
        {
            "sport_slug": sport_slug,
            "version": registry_row.version,
            "artefact_path": registry_row.artefact_path.replace("\\", "/").rsplit("/", 1)[-1],
            "accuracy": registry_row.accuracy,
            "rps_score": registry_row.rps_score,
            "roi_simulation": registry_row.roi_simulation,
            "trained_at": registry_row.trained_at.isoformat(),
        }
        for registry_row, sport_slug in sorted(rows, key=lambda pair: pair[1])
    ]
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"  wrote {MANIFEST.name} ({len(manifest)} models)")
    print("done — commit ml/artifacts/deployed/ so the image contains them")
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="actually copy the artefacts")
    asyncio.run(main(parser.parse_args().confirm))
