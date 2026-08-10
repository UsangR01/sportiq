"""push_tickets: record Expo ticket ids so receipts can be checked

Revision ID: 30eb323b969e
Revises: f1c7a2d9e483
Create Date: 2026-08-10 13:55:04.712400

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "30eb323b969e"
down_revision: str | None = "f1c7a2d9e483"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Records Expo push ticket ids so their delivery receipts can be checked.

    Autogenerate also wanted to make watchlist_items.created_at NOT NULL. That is unrelated
    pre-existing drift between the model and the database, not part of this change, and it has
    been removed rather than smuggled into a push-receipts migration. Worth fixing on its own.
    """
    op.create_table(
        "push_tickets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.String(length=100), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_push_tickets_created", "push_tickets", ["created_at"], unique=False)
    op.create_index(op.f("ix_push_tickets_user_id"), "push_tickets", ["user_id"], unique=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    op.drop_index(op.f("ix_push_tickets_user_id"), table_name="push_tickets")
    op.drop_index("ix_push_tickets_created", table_name="push_tickets")
    op.drop_table("push_tickets")
    # ### end Alembic commands ###
