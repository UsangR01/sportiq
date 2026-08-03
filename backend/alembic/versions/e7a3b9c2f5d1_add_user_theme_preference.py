"""add user_preferences.theme_preference

Appearance (light/dark/system) chosen in the mobile app's Profile screen. Stored on the
account so it follows the user across devices; guests keep theirs on-device only, since a
guest session is device-bound and has nothing to sync to.

Note the stored values are the Python enum MEMBER NAMES (uppercase), matching this schema's
existing convention — see 29c85029ecef_init_schema.py's own
`sa.Enum("DECIMAL", "FRACTIONAL", "AMERICAN", name="odds_format")`. Using lowercase here
would raise InvalidTextRepresentationError on the first write.

Non-nullable with a SYSTEM server_default: "follow the OS" is a real choice rather than a
missing one, and the default backfills every existing row correctly without a data migration.

Revision ID: e7a3b9c2f5d1
Revises: d5b1c7e3a9f2
"""

import sqlalchemy as sa

from alembic import op

revision = "e7a3b9c2f5d1"
down_revision = "d5b1c7e3a9f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    theme = sa.Enum("LIGHT", "DARK", "SYSTEM", name="theme_preference")
    theme.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "user_preferences",
        sa.Column("theme_preference", theme, nullable=False, server_default="SYSTEM"),
    )


def downgrade() -> None:
    op.drop_column("user_preferences", "theme_preference")
    sa.Enum(name="theme_preference").drop(op.get_bind(), checkfirst=True)
