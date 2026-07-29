"""add double chance / goals+corners totals odds columns, and prediction xg/corners columns

Revision ID: a7c4e2f1b8d3
Revises: f3a8b1c9d4e2
Create Date: 2026-07-30 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c4e2f1b8d3"
down_revision: str | None = "f3a8b1c9d4e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New Postgres enum values must be committed outside the migration's own transaction —
    # same constraint already hit by the InjurySource.API_FOOTBALL addition (see CLAUDE.md).
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE odds_market ADD VALUE IF NOT EXISTS 'DOUBLE_CHANCE'")
        op.execute("ALTER TYPE odds_market ADD VALUE IF NOT EXISTS 'CORNERS_TOTAL'")

    # For the (pre-existing but never-used) TOTAL market and the new DOUBLE_CHANCE/
    # CORNERS_TOTAL markets: `line` is the numeric total line (e.g. 2.5, 9.5) — null for
    # h2h/double_chance, which have no line. `over_odds`/`under_odds` are the totals-market
    # prices; double_chance reuses the existing home_odds/away_odds columns (a genuine 2-way
    # market, same shape) rather than adding yet more columns.
    op.add_column("odds", sa.Column("line", sa.Float(), nullable=True))
    op.add_column("odds", sa.Column("over_odds", sa.Float(), nullable=True))
    op.add_column("odds", sa.Column("under_odds", sa.Float(), nullable=True))

    # Layer 1's own expected-goals output, previously computed but discarded after Layer 2 ran
    # — now persisted so Over/Under-goals probabilities can be derived at read time without
    # re-running inference. corners_xg_* are the new corners-Poisson-regressor outputs (see
    # app/models_ml/football.py) — both pairs nullable since NBA predictions (and any football
    # prediction made before this migration) have neither.
    op.add_column("predictions", sa.Column("xg_home", sa.Float(), nullable=True))
    op.add_column("predictions", sa.Column("xg_away", sa.Float(), nullable=True))
    op.add_column("predictions", sa.Column("corners_xg_home", sa.Float(), nullable=True))
    op.add_column("predictions", sa.Column("corners_xg_away", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("predictions", "corners_xg_away")
    op.drop_column("predictions", "corners_xg_home")
    op.drop_column("predictions", "xg_away")
    op.drop_column("predictions", "xg_home")
    op.drop_column("odds", "under_odds")
    op.drop_column("odds", "over_odds")
    op.drop_column("odds", "line")
    # Postgres has no DROP VALUE for enums — leaving DOUBLE_CHANCE/CORNERS_TOTAL in place on
    # downgrade is the same accepted limitation as the existing API_FOOTBALL enum-value add.
