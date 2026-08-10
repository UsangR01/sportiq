"""predictions.kind: separate real forecasts from retrodictions

Revision ID: 8ab44e834f9f
Revises: 30eb323b969e
Create Date: 2026-08-10 14:50:03.785391

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8ab44e834f9f"
down_revision: str | None = "30eb323b969e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Adds predictions.kind and classifies existing rows as honestly as the data allows.

    Both prediction paths wrote to the same table with nothing to tell them apart, so any
    performance measurement over `predictions` mixed genuine forecasts with retrodictions
    produced after the result was known.

    THE BACKFILL IS DELIBERATELY INCOMPLETE. `created_at < kickoff_utc` proves a row was
    written before the match and is therefore a real forecast — nothing regenerates a
    prediction backwards in time, so that direction is safe. The reverse does NOT hold: 91
    football predictions were regenerated on 2026-08-10 after a model change, resetting their
    created_at to well after those fixtures kicked off. Rows on that side are a mix of real
    retrodictions and regenerated forecasts, and there is no stored fact that separates them.

    They are therefore left UNKNOWN rather than assumed to be retrodictions. Guessing would
    silently move rows into whichever bucket the reader trusts, and an inflated track record is
    a far more expensive mistake than a smaller honest one.
    """
    # Created explicitly: add_column does not emit CREATE TYPE for a new enum on this
    # driver, so the ALTER TABLE arrives before the type exists.
    prediction_kind = postgresql.ENUM(
        "PRE_MATCH", "RETRODICTION", "UNKNOWN", name="prediction_kind"
    )
    prediction_kind.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "predictions",
        sa.Column("kind", prediction_kind, server_default="UNKNOWN", nullable=False),
    )
    op.execute("""
        UPDATE predictions
           SET kind = 'PRE_MATCH'
          FROM fixtures
         WHERE fixtures.id = predictions.fixture_id
           AND predictions.created_at < fixtures.kickoff_utc
        """)


def downgrade() -> None:
    op.drop_column("predictions", "kind")
    op.execute("DROP TYPE IF EXISTS prediction_kind")
