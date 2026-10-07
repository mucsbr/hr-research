"""Finite classified retention with first-activation grace and durable journals.

Revision ID: b25d0c8f3142
Revises: a14c9b7e2031
"""
from datetime import datetime, timedelta, timezone
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b25d0c8f3142"
down_revision = "a14c9b7e2031"
branch_labels = None
depends_on = None


def entity_columns():
    return [sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade():
    for table in ("raw_documents", "analysis_reports"):
        op.add_column(table, sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(table, sa.Column("retention", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
        op.create_index("ix_" + table + "_cleared_at", table, ["cleared_at"])
        op.alter_column(table, "retention", server_default=None)
    op.add_column("task_runs", sa.Column("logs_cleared_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("task_runs", sa.Column("log_retention", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.alter_column("task_runs", "log_retention", server_default=None)
    policy = op.create_table("retention_policies", *entity_columns(),
        sa.Column("category", sa.String(40), nullable=False, unique=True),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False), sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("grace_until", sa.DateTime(timezone=True), nullable=False))
    op.create_table("retention_previews", *entity_columns(),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("items", postgresql.JSONB(), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False))
    op.create_index("ix_retention_previews_expires_at", "retention_previews", ["expires_at"])
    op.create_table("retention_runs", *entity_columns(),
        sa.Column("preview_id", sa.Uuid(), sa.ForeignKey("retention_previews.id"), nullable=False, unique=True),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
        sa.Column("trigger", sa.String(20), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("policy_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("items", postgresql.JSONB(), nullable=False), sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)), sa.Column("error_message", sa.Text()))
    op.create_index("ix_retention_runs_status", "retention_runs", ["status"])
    stamp = datetime.now(timezone.utc)
    op.bulk_insert(policy, [{"id": uuid.uuid4(), "created_at": stamp, "updated_at": stamp,
                            "category": category, "retention_days": days, "enabled": True,
                            "revision": 1, "grace_until": stamp + timedelta(days=7)}
                           for category, days in {"raw_documents": 90, "reports": 180,
                                                  "task_logs": 90, "expired_sessions": 7}.items()])


def downgrade():
    op.drop_table("retention_runs")
    op.drop_table("retention_previews")
    op.drop_table("retention_policies")
    op.drop_column("task_runs", "log_retention")
    op.drop_column("task_runs", "logs_cleared_at")
    for table in ("raw_documents", "analysis_reports"):
        op.drop_index("ix_" + table + "_cleared_at", table_name=table)
        op.drop_column(table, "retention")
        op.drop_column(table, "cleared_at")
