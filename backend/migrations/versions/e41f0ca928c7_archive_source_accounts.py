"""Preserve historical accounts while excluding archived records from active use."""
from alembic import op
import sqlalchemy as sa

revision = "e41f0ca928c7"
down_revision = "d39a7e6c8102"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("source_accounts", sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.alter_column("source_accounts", "archived", server_default=None)


def downgrade():
    op.drop_column("source_accounts", "archived")
