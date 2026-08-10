"""watchlist_items.created_at NOT NULL

Revision ID: ba36f6870577
Revises: e065ad8ee476
Create Date: 2026-08-11 00:21:13.558243

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ba36f6870577"
down_revision: str | None = "e065ad8ee476"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Closes a model/database drift that outlived three unrelated migrations.

    WatchlistItem has always declared created_at non-nullable with a server_default, but the
    column was created nullable. Autogenerate therefore proposed this fix inside the
    push-receipts, prediction-kind and pick-snapshots migrations in turn, and it was stripped
    from each -- correctly, since an unrelated schema change riding along in a migration named
    for something else is how surprises get shipped. Stripping it three times without ever
    doing it is its own failure mode, so it gets its own migration here.

    Verified before applying: zero rows have a NULL created_at, so the constraint cannot fail
    on existing data.
    """
    op.alter_column(
        "watchlist_items",
        "created_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        nullable=False,
        existing_server_default=sa.text("now()"),
    )


def downgrade() -> None:
    op.alter_column(
        "watchlist_items",
        "created_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        nullable=True,
        existing_server_default=sa.text("now()"),
    )
