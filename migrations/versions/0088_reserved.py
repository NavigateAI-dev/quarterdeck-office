"""Reserved: an optional connector that is not part of this release.

Kept as an empty step so the revision chain stays unbroken.

Revision ID: 0088
Revises: 0087
"""

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
