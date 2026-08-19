"""add team_features.rank_position

Tennis only. The player's actual ATP/WTA ranking POSITION, alongside the ranking POINTS the
column next to it already held. Both are returned by the same BallDontLie /rankings response,
so capturing position costs no extra API call.

It exists because raw points are wildly non-linear in position -- measured on the live table,
dropping ten places costs 8,720 points at #1 and 119 points at #50 -- so a raw points
subtraction cannot separate "#3 v #5" from "#40 v #90". That measurably capped how far form
could ever move a pick: the served model agreed with "back the higher-ranked player" 88.4% of
the time, and 100% of the time once the gap passed 2,000 points. See train_tennis.py's
pre-registration block.

Nullable with NO backfill, matching feature_completeness's own rationale: predictions made
before this column existed genuinely have no measurement, and inventing one retroactively
would hide exactly what the completeness floor is there to expose.

Revision ID: b8d4e6f1a207
Revises: faa1ecc4c1ac
"""

import sqlalchemy as sa

from alembic import op

revision: str = "b8d4e6f1a207"
down_revision: str | None = "faa1ecc4c1ac"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("team_features", sa.Column("rank_position", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("team_features", "rank_position")
