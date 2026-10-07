"""Structured news subscriptions and dated policy reminders.

Revision ID: a14c9b7e2031
Revises: 7c8ba429a6ea
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a14c9b7e2031"
down_revision = "7c8ba429a6ea"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("notification_rules", sa.Column("news_filters", postgresql.JSONB(), nullable=False,
        server_default=sa.text("'{\"regions\":[],\"industries\":[],\"topics\":[]}'::jsonb")))
    op.alter_column("notification_rules", "news_filters", server_default=None)
    op.create_table("news_policy_reminders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("context", postgresql.JSONB(), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["item_id"], ["news_items.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["notification_events.id"]),
        sa.UniqueConstraint("item_id", "effective_date"))
    op.create_index("ix_news_policy_reminders_item_id", "news_policy_reminders", ["item_id"])
    op.create_index("ix_news_policy_reminders_due", "news_policy_reminders", ["status", "scheduled_for"])


def downgrade():
    op.drop_index("ix_news_policy_reminders_due", table_name="news_policy_reminders")
    op.drop_index("ix_news_policy_reminders_item_id", table_name="news_policy_reminders")
    op.drop_table("news_policy_reminders")
    op.drop_column("notification_rules", "news_filters")
