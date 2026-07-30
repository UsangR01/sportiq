"""add postponed value to fixture_status enum

Revision ID: c9e1a4f7d2b6
Revises: b2e6f4a9c1d7
Create Date: 2026-07-30 09:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9e1a4f7d2b6"
down_revision: str | None = "b2e6f4a9c1d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Postgres requires ALTER TYPE ... ADD VALUE to run outside the migration's normal
    # transaction block — same autocommit_block() pattern as 6a3f9c1e2b7d's API_FOOTBALL
    # addition. The stored value must match the Python enum's MEMBER NAME (uppercase), not
    # its .value string, per that same migration's confirmed-live finding.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE fixture_status ADD VALUE IF NOT EXISTS 'POSTPONED'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE — recreating the type isn't worth the risk
    # for a value that's simply unused going forward, same call as 6a3f9c1e2b7d.
    pass
