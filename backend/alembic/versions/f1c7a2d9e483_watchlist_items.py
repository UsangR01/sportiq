"""add watchlist_items (PRD PICK-07, TDD §5.4)

Revision ID: f1c7a2d9e483
Revises: d5b8e1c4a397
Create Date: 2026-08-04

Not in the TDD §2.1 schema listing, but §5.4's T-60-minute kickoff reminder cannot exist
without it — app/workers/notify_users.py:notify_kickoff_reminder has been a NotImplementedError
purely because there was nowhere to read "who wants telling about this fixture" from.

Both foreign keys cascade on delete: a watchlist row is meaningless once its user or its
fixture is gone, and the tennis purge earlier today showed exactly why dependent rows should
not be left to be deleted by hand.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "f1c7a2d9e483"
down_revision = "d5b8e1c4a397"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watchlist_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "fixture_id",
            UUID(as_uuid=True),
            sa.ForeignKey("fixtures.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True),
        # Saving the same fixture twice is a no-op rather than a second row, so a double tap
        # cannot produce two reminders.
        sa.UniqueConstraint("user_id", "fixture_id", name="uq_watchlist_user_fixture"),
    )
    op.create_index("ix_watchlist_items_user_id", "watchlist_items", ["user_id"])
    # The reminder worker's own query shape: every watcher of one fixture.
    op.create_index("ix_watchlist_fixture", "watchlist_items", ["fixture_id"])


def downgrade() -> None:
    op.drop_index("ix_watchlist_fixture", table_name="watchlist_items")
    op.drop_index("ix_watchlist_items_user_id", table_name="watchlist_items")
    op.drop_table("watchlist_items")
