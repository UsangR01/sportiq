"""add fixture_live_state home_corners/away_corners

Revision ID: b2e6f4a9c1d7
Revises: a7c4e2f1b8d3
Create Date: 2026-07-30 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2e6f4a9c1d7"
down_revision: str | None = "a7c4e2f1b8d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("fixture_live_state", sa.Column("home_corners", sa.Integer(), nullable=True))
    op.add_column("fixture_live_state", sa.Column("away_corners", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("fixture_live_state", "away_corners")
    op.drop_column("fixture_live_state", "home_corners")
