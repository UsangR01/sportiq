"""add predictions.driver_contributions

Exact TreeSHAP contributions recorded at inference time, so a fixture panel can say WHICH real
factors moved the call rather than only what the call was.

DELIBERATELY NULLABLE WITH NO BACKFILL. The contributions are a function of the feature vector
as it stood at the moment of prediction, and that vector is not reconstructible afterwards --
form, Elo and odds coverage have all moved since. Inventing values for existing rows would
explain those fixtures with data that postdates them, which is the same hindsight problem
`predictions.kind` exists to keep out of the accuracy figures. Old rows carry nothing and the
API says so.

Revision ID: d7b3f1a9c264
Revises: c5a71e93f4b8
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d7b3f1a9c264"
down_revision = "c5a71e93f4b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "predictions",
        sa.Column("driver_contributions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("predictions", "driver_contributions")
