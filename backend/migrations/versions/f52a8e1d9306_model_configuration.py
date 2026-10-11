"""Encrypted administrator-owned model settings, without rewriting call history."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f52a8e1d9306"
down_revision = "e41f0ca928c7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("model_configurations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(40), nullable=False, unique=True),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("encrypted_api_key", sa.Text()),
        sa.Column("revision", sa.Integer(), nullable=False))


def downgrade():
    op.drop_table("model_configurations")
