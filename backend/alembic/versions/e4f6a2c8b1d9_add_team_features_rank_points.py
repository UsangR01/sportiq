"""add team_features rank_points (tennis)

Revision ID: e4f6a2c8b1d9
Revises: c9e1a4f7d2b6
Create Date: 2026-07-31 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4f6a2c8b1d9"
down_revision: str | None = "c9e1a4f7d2b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("team_features", sa.Column("rank_points", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("team_features", "rank_points")
