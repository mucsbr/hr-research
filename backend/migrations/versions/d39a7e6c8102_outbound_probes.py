"""Outbound desktop probes, profile leases and encrypted command receipts.

Revision ID: d39a7e6c8102
Revises: b25d0c8f3142
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d39a7e6c8102"
down_revision = "b25d0c8f3142"
branch_labels = None
depends_on = None


def entity():
    return [sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def fk(name, target, nullable=False):
    return sa.Column(name, sa.Uuid(), sa.ForeignKey(target), nullable=nullable)


def upgrade():
    op.create_table("probe_pairings", *entity(), fk("owner_user_id", "users.id"),
                    sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
                    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("consumed_at", sa.DateTime(timezone=True)))
    op.create_table("probe_devices", *entity(), fk("owner_user_id", "users.id"),
                    sa.Column("name", sa.String(120), nullable=False),
                    sa.Column("platform", sa.String(20), nullable=False),
                    sa.Column("architecture", sa.String(20), nullable=False),
                    sa.Column("version", sa.String(40), nullable=False),
                    sa.Column("protocol_version", sa.String(20), nullable=False),
                    sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
                    sa.Column("capabilities", postgresql.JSONB(), nullable=False),
                    sa.Column("enabled", sa.Boolean(), nullable=False),
                    sa.Column("paused", sa.Boolean(), nullable=False),
                    sa.Column("connection_id", sa.Uuid()),
                    sa.Column("last_seen_at", sa.DateTime(timezone=True)),
                    sa.Column("chrome_version", sa.String(100)))
    op.create_index("ix_probe_devices_owner_user_id", "probe_devices", ["owner_user_id"])
    op.create_table("probe_profiles", *entity(), fk("device_id", "probe_devices.id"),
                    sa.Column("name", sa.String(120), nullable=False),
                    sa.Column("enabled", sa.Boolean(), nullable=False))
    op.create_index("ix_probe_profiles_device_id", "probe_profiles", ["device_id"])
    op.create_table("probe_account_bindings", *entity(), fk("account_id", "source_accounts.id"),
                    fk("profile_id", "probe_profiles.id"),
                    sa.Column("revision", sa.Uuid(), nullable=False),
                    sa.UniqueConstraint("account_id"))
    op.create_index("ix_probe_account_bindings_profile_id", "probe_account_bindings", ["profile_id"])
    op.create_table("probe_browser_sessions", *entity(), fk("profile_id", "probe_profiles.id"),
                    fk("account_id", "source_accounts.id"), fk("task_id", "task_runs.id", True),
                    sa.Column("attempt", sa.Integer()), sa.Column("kind", sa.String(30), nullable=False),
                    sa.Column("binding_revision", sa.Uuid(), nullable=False),
                    sa.Column("status", sa.String(30), nullable=False),
                    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_probe_browser_sessions_profile_id", "probe_browser_sessions", ["profile_id"])
    op.create_index("ix_probe_browser_sessions_task_id", "probe_browser_sessions", ["task_id"])
    op.create_index("uq_probe_active_profile", "probe_browser_sessions", ["profile_id"], unique=True,
                    postgresql_where=sa.text("status = 'active'"))
    op.create_table("probe_commands", *entity(), fk("device_id", "probe_devices.id"),
                    fk("session_id", "probe_browser_sessions.id", True),
                    sa.Column("operation", sa.String(40), nullable=False),
                    sa.Column("status", sa.String(30), nullable=False),
                    sa.Column("payload_cipher", sa.Text()), sa.Column("result_cipher", sa.Text()),
                    sa.Column("result_digest", sa.String(64)), sa.Column("error_code", sa.String(80)),
                    sa.Column("connection_id", sa.Uuid()),
                    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("delivered_at", sa.DateTime(timezone=True)),
                    sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.create_index("ix_probe_commands_device_id", "probe_commands", ["device_id"])
    op.create_index("ix_probe_commands_session_id", "probe_commands", ["session_id"])
    op.create_index("ix_probe_command_dispatch", "probe_commands", ["device_id", "status", "created_at"])


def downgrade():
    for name in ("probe_commands", "probe_browser_sessions", "probe_account_bindings",
                 "probe_profiles", "probe_devices", "probe_pairings"):
        op.drop_table(name)
