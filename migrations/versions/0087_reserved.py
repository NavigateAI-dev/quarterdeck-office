"""Reserved: an optional connector that is not part of this release.

Kept as an empty step so the revision chain stays unbroken.

Revision ID: 0087
Revises: 0086
"""

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
