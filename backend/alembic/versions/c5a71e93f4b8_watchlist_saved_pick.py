"""record the pick a user SAW when they saved a fixture

Revision ID: c5a71e93f4b8
Revises: b8d4e6f1a207

best_pick is recomputed on every request and never stored, so a saved fixture's card can read
differently every time it is opened. Reported twice in one day -- a WNBA pick moving 59% -> 66%
overnight, and a La Liga card that showed "over 1.5 goals" one day and a double chance the next
-- and experienced as the app changing its mind after the user had acted on it.

The churn itself is mostly legitimate (books price late, and the market feeds the model), so
the fix is not to freeze what everyone sees. It is to freeze what THIS user saw, at the moment
they chose to act, while the feed stays live for everyone still deciding. That is the only
version that gives a stable personal record without showing a first-time visitor a stale
number an hour before kickoff.

All columns nullable with no backfill: rows saved before this existed genuinely have no record
of what was shown, and inventing one would be a fabricated receipt. A null saved_market simply
means "saved before we started recording this", which the client renders as an ordinary
watchlist entry.
"""

import sqlalchemy as sa

from alembic import op

revision: str = "c5a71e93f4b8"
down_revision: str | None = "b8d4e6f1a207"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("watchlist_items", sa.Column("saved_market", sa.String(32), nullable=True))
    op.add_column("watchlist_items", sa.Column("saved_selection", sa.String(16), nullable=True))
    op.add_column("watchlist_items", sa.Column("saved_line", sa.Float(), nullable=True))
    op.add_column("watchlist_items", sa.Column("saved_probability", sa.Float(), nullable=True))
    op.add_column("watchlist_items", sa.Column("saved_odds", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("watchlist_items", "saved_odds")
    op.drop_column("watchlist_items", "saved_probability")
    op.drop_column("watchlist_items", "saved_line")
    op.drop_column("watchlist_items", "saved_selection")
    op.drop_column("watchlist_items", "saved_market")
