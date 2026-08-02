"""add predictions.feature_completeness

Fraction of the model's feature vector that had a real (non-null) value at inference time.

A prediction built from a mostly-empty feature vector isn't wrong, but it carries far less
information than one built from a full vector — and the feed currently renders both with
identical authority. The concrete case that motivated this: 26% of retrodicted ATP fixtures
came out at exactly 0.562, because those players' prior-match history was largely missing and
the model was effectively falling back on the base rate. Recording completeness lets the client
distinguish a confident 60% from a 60% that means "we know almost nothing here".

Nullable, with no backfill: predictions made before this existed genuinely have no measurement,
and inventing one retroactively would defeat the purpose.

Revision ID: c4f8a2b6e1d3
Revises: a1d5c3e7b904
"""

import sqlalchemy as sa

from alembic import op

revision = "c4f8a2b6e1d3"
down_revision = "a1d5c3e7b904"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("predictions", sa.Column("feature_completeness", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("predictions", "feature_completeness")
