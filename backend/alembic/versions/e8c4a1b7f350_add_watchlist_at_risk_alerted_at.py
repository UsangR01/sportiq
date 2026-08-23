"""add watchlist_items.at_risk_alerted_at

Dedupe stamp for the in-play at-risk alert (design spec §4.1). Live scores poll every five
minutes and a pick that goes at-risk usually stays at-risk, so without a stamp one bad scoreline
would send roughly six notifications about the same match.

Separate from `reminded_at` rather than reusing it: they are different promises — one says a
match is about to start, the other that a pick has started going wrong — and a user who got the
kick-off reminder must still be able to get the at-risk alert.

Revision ID: e8c4a1b7f350
Revises: d7b3f1a9c264
"""

import sqlalchemy as sa

from alembic import op

revision = "e8c4a1b7f350"
down_revision = "d7b3f1a9c264"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "watchlist_items",
        sa.Column("at_risk_alerted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("watchlist_items", "at_risk_alerted_at")
