"""Activate a registered model version — promotion AND revocation use the same lever.

Training scripts activate on promotion runs, but two real situations need activation outside
a training run: promoting an arm that was deliberately trained with --no-activate (the market-
features arm, football_xgb_v20260818200900, is the motivating case), and REVOKING a promotion
by reactivating the incumbent. Both have been done by hand-typed SQL in a Render shell before,
which is exactly the kind of step that gets typo'd under pressure.

Demotes every other active row for the same sport, then activates the named version — the
same invariant _register_model maintains (never zero, never two active rows per sport).

Dry run by default. Works locally and in a Render shell:

    PYTHONPATH=. python scripts/activate_model.py football_xgb_v20260818200900
    PYTHONPATH=. python scripts/activate_model.py football_xgb_v20260818200900 --confirm
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.database import async_session_factory  # noqa: E402
from app.predictions.models import ModelRegistry  # noqa: E402

# Imported for SQLAlchemy FK metadata only — flushing ModelRegistry resolves the sports
# table through the mapper registry, and a script that never imports Sport crashes with
# NoReferencedTableError (same trap repair_wnba_team_names.py documents).
from app.sports.models import Sport  # noqa: E402, F401


async def main(version: str, confirm: bool) -> None:
    async with async_session_factory() as db:
        target = (
            await db.execute(select(ModelRegistry).where(ModelRegistry.version == version))
        ).scalar_one_or_none()
        if target is None:
            print(f"no registry row for version {version!r}")
            return
        others = (
            (
                await db.execute(
                    select(ModelRegistry).where(
                        ModelRegistry.sport_id == target.sport_id,
                        ModelRegistry.is_active.is_(True),
                        ModelRegistry.version != version,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in others:
            print(f"  demote  {row.version}")
            if confirm:
                row.is_active = False
        print(f"  activate {target.version} (currently is_active={target.is_active})")
        if confirm:
            target.is_active = True
            await db.commit()
            print("WROTE — ingest re-queues predictions on the version change automatically")
        else:
            print("dry run — pass --confirm to write")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.version, args.confirm))
