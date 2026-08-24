"""add fixtures.tournament_end_utc

Bounds a PLACEHOLDER kickoff. A tennis match with no scheduled_time inherits its tournament's
START date, and the live-score sweep rolls such a fixture forward to today once its day has
passed -- right for a real match still to be played, unbounded for one that never will be.

Measured: a Toby Samuel v J.J. Wolf phantom stamped 11 August was still riding the feed on the
24th. BallDontLie keeps reporting it `scheduled` and never withdraws it, so the vanished-fixture
reconciliation cannot see it (the id is still in the payload) and the clock sweep exempts
placeholder kickoffs by design.

The tournament's own close is the exact point after which the match cannot happen, and the
provider already embeds it in every match response.

Revision ID: f1a7c3d9e820
Revises: e8c4a1b7f350
"""

import sqlalchemy as sa

from alembic import op

revision = "f1a7c3d9e820"
down_revision = "e8c4a1b7f350"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fixtures", sa.Column("tournament_end_utc", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("fixtures", "tournament_end_utc")
