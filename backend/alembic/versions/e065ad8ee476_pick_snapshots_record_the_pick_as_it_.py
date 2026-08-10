"""pick_snapshots: record the pick as it was shown

Revision ID: e065ad8ee476
Revises: 8ab44e834f9f
Create Date: 2026-08-10 15:37:31.151332

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e065ad8ee476"
down_revision: str | None = "8ab44e834f9f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Records the pick as it was SHOWN, so product performance becomes measurable.

    Until now best_pick was computed per request and never stored, so hit rate, ROI and CLV over
    shown picks could not be graded after the fact without recomputing them against today's odds
    and today's guards — a different product than users saw. See docs/history-metrics-spec.md.

    Autogenerate again wanted watchlist_items.created_at NOT NULL. Stripped, as in the two
    migrations before this: it is unrelated pre-existing drift and belongs in its own migration
    rather than riding along in three consecutive unrelated ones. It stays on the backlog.
    """
    op.create_table(
        "pick_snapshots",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("fixture_id", sa.UUID(), nullable=False),
        sa.Column("prediction_id", sa.UUID(), nullable=False),
        sa.Column("model_version", sa.String(length=100), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("selection", sa.String(length=32), nullable=False),
        sa.Column("line", sa.Float(), nullable=True),
        sa.Column("probability", sa.Float(), nullable=False),
        sa.Column("odds", sa.Float(), nullable=True),
        sa.Column("feature_completeness", sa.Float(), nullable=True),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("hours_before_kickoff", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["fixture_id"], ["fixtures.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["prediction_id"], ["predictions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fixture_id", name="uq_pick_snapshots_fixture"),
    )
    op.create_index("ix_pick_snapshots_captured", "pick_snapshots", ["captured_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_pick_snapshots_captured", table_name="pick_snapshots")
    op.drop_table("pick_snapshots")
