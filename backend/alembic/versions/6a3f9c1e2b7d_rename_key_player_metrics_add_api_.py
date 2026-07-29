"""rename team_key_players ws_48/per to rank_metric/combined_metric, add api_football injury source

Revision ID: 6a3f9c1e2b7d
Revises: d7cdee97c00f
Create Date: 2026-07-29 10:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6a3f9c1e2b7d"
down_revision: str | None = "d7cdee97c00f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Pure rename — makes team_key_players genuinely sport-agnostic (see
    # app/fixtures/models.py:TeamKeyPlayer docstring). Zero behavior change for existing NBA
    # rows/readers once app/models_ml/nba_key_players.py's write/read paths are updated to
    # match (same commit).
    op.alter_column("team_key_players", "ws_48", new_column_name="rank_metric")
    op.alter_column("team_key_players", "per", new_column_name="combined_metric")

    # Postgres requires ALTER TYPE ... ADD VALUE to run outside the migration's normal
    # transaction block (a new enum value can't be used in the same transaction that adds it,
    # and older Postgres versions reject it in a transaction at all) — autocommit_block()
    # handles this the way Alembic's own docs recommend.
    # SQLAlchemy's Enum column stores the Python enum MEMBER NAME (uppercase, e.g.
    # "ROTOWIRE"/"BALLDONTLIE" — see alembic/versions/29c85029ecef_init_schema.py's original
    # sa.Enum("ROTOWIRE", "BALLDONTLIE", ...) call), not the .value string, so the new value
    # must be "API_FOOTBALL" to match — confirmed live: inserting lowercase "api_football"
    # raised InvalidTextRepresentationError.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE injury_source ADD VALUE IF NOT EXISTS 'API_FOOTBALL'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE — downgrading the enum would require
    # recreating the type entirely, which isn't worth the risk for a value that's simply
    # unused going forward. The column rename downgrade is safe and reversible.
    op.alter_column("team_key_players", "rank_metric", new_column_name="ws_48")
    op.alter_column("team_key_players", "combined_metric", new_column_name="per")
