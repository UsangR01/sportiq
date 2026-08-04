"""store models_registry.artefact_path as a filename, not an absolute path

Revision ID: d5b8e1c4a397
Revises: e7a3b9c2f5d1
Create Date: 2026-08-04

Every row registered before this held an absolute Windows path
(C:\\Users\\...\\ml\\artifacts\\football_xgb_*.joblib) because the training scripts wrote
str(artefact_path). That cannot resolve inside a Linux container, so the first deploy would
have failed to load any model at all — and it defeats TDD §3.1's design that promoting a model
is a DB update rather than a redeploy, since the path is only meaningful on the machine that
trained it.

The path is reduced to its filename here; app/models_ml/base.py:resolve_artefact_path joins it
to settings.models_path (MODELS_DIR, defaulting to the repo's ml/artifacts). That function
still accepts a full path that happens to exist, so this migration is not a flag day — a row
missed here keeps working on the machine that wrote it.

Data-only: the column type and constraints are unchanged.

The downgrade cannot restore the original directory (it is not recorded anywhere and differs
per machine), so it is deliberately a no-op rather than a lossy guess that would write a path
belonging to whichever host ran it.
"""

import sqlalchemy as sa

from alembic import op

revision = "d5b8e1c4a397"
down_revision = "e7a3b9c2f5d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Handles both separators: rows were written on Windows, but a mixed-history database
    # could hold either. Rows already stored as a bare filename match neither and are skipped.
    op.execute(sa.text(r"""
            UPDATE models_registry
            SET artefact_path = regexp_replace(artefact_path, '^.*[\\/]', '')
            WHERE artefact_path ~ '[\\/]'
            """))


def downgrade() -> None:
    """Intentionally a no-op — see the module docstring."""
