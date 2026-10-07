"""Fence adapter publications and rollbacks across ABA version changes."""
from alembic import op
import sqlalchemy as sa

revision = "a918eb1a67d2"
down_revision = "9d924bc617c7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("adapter_registries", sa.Column("release_generation", sa.Integer(),
                                                nullable=False, server_default="0"))
    op.alter_column("adapter_registries", "release_generation", server_default=None)
    # Old unissued candidates have no reliable generation: do not invent one.
    op.add_column("adapter_releases", sa.Column("replaces_generation", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("adapter_releases", "replaces_generation")
    op.drop_column("adapter_registries", "release_generation")
