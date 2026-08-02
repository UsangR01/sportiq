"""add fixture_live_state.result_type and fixtures tournament fields

Two independent, tennis-driven additions (both nullable, both no-ops for football/NBA):

1. `fixture_live_state.result_type` — NULL for a normally-played-out result, "retired"/
   "walkover" for one that ended without being played out. Added after a real, user-reported
   bug: a genuine mid-match retirement was being stored as an impossible 1-1 tennis scoreline
   AND inverting the win/loss verdict on a prediction the model had actually got right. See
   app/adapters/balldontlie_tennis.py:_match_result_type — this has to be inferred from the
   score structurally, because the provider reports real retirements as match_status
   "finished" with no retirement marker at all.

2. `fixtures.tournament_name` / `.tournament_surface` / `.tournament_location` — a tennis tour
   (ATP/WTA) is a single League row, but the feed needs to group by TOURNAMENT so a user can
   find the right event in a betting app. Denormalised onto the fixture since these arrive
   already embedded in every match response. `tournament_location` is a CITY, not a country.

Revision ID: a1d5c3e7b904
Revises: e4f6a2c8b1d9
"""

import sqlalchemy as sa

from alembic import op

revision = "a1d5c3e7b904"
down_revision = "e4f6a2c8b1d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fixture_live_state", sa.Column("result_type", sa.String(length=20), nullable=True)
    )
    op.add_column("fixtures", sa.Column("tournament_name", sa.String(length=200), nullable=True))
    op.add_column("fixtures", sa.Column("tournament_surface", sa.String(length=30), nullable=True))
    op.add_column(
        "fixtures", sa.Column("tournament_location", sa.String(length=120), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("fixtures", "tournament_location")
    op.drop_column("fixtures", "tournament_surface")
    op.drop_column("fixtures", "tournament_name")
    op.drop_column("fixture_live_state", "result_type")
