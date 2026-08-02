"""add fixtures.kickoff_is_estimated

True when kickoff_utc was INFERRED rather than reported by the provider.

Tennis made this necessary. Measured across a full real ATP tournament (600 matches), 570 had
no scheduled_time at all and 17 of the remaining 30 were exactly midnight — only ~2% carried a
genuine kickoff time. The adapter fell back to the tournament's start date, which gave every
match in a 12-day draw the same timestamp. That produced two visible bugs at once: every match
displayed the same wrong time (midnight UTC, i.e. 01:00 BST), and matches from later rounds
appeared on today's schedule, so users could not find them on any real platform.

Fabricating a kickoff contradicts this codebase's standing rule of never inventing a neutral
value. Flagging it keeps the fixture usable while letting the client say "Time TBC" instead of
asserting precision we do not have.

Non-nullable with server_default false: football/NBA kickoffs are genuinely reported, so
existing rows are correctly marked as not-estimated.

Revision ID: d5b1c7e3a9f2
Revises: c4f8a2b6e1d3
"""

import sqlalchemy as sa

from alembic import op

revision = "d5b1c7e3a9f2"
down_revision = "c4f8a2b6e1d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fixtures",
        sa.Column(
            "kickoff_is_estimated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("fixtures", "kickoff_is_estimated")
