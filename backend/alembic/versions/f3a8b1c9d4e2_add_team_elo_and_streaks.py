"""add team elo_rating and team_features streaks

Revision ID: f3a8b1c9d4e2
Revises: 6a3f9c1e2b7d
Create Date: 2026-07-29 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a8b1c9d4e2"
down_revision: str | None = "6a3f9c1e2b7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("teams", sa.Column("elo_rating", sa.Float(), nullable=True))
    op.add_column("team_features", sa.Column("win_streak", sa.Float(), nullable=True))
    op.add_column("team_features", sa.Column("losing_streak", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("team_features", "losing_streak")
    op.drop_column("team_features", "win_streak")
    op.drop_column("teams", "elo_rating")
