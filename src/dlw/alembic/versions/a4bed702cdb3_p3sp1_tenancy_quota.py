"""p3sp1 tenancy quota

Revision ID: a4bed702cdb3
Revises: 6f37b72630ce
Create Date: 2026-05-18 23:26:11.755077

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a4bed702cdb3'
down_revision: str | None = '6f37b72630ce'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_records",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_usage_tenant_metric_time", "usage_records",
                    ["tenant_id", "metric", "occurred_at"])
    op.create_table(
        "quota_snapshots",
        sa.Column("tenant_id", sa.BigInteger(),
                  sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("bytes_used_month", sa.BigInteger(),
                  server_default="0", nullable=False),
        sa.Column("storage_gb_used", sa.BigInteger(),
                  server_default="0", nullable=False),
        sa.Column("concurrent_tasks", sa.Integer(),
                  server_default="0", nullable=False),
        sa.Column("last_recomputed_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "casbin_rule",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("ptype", sa.String(8), nullable=False),
        *[sa.Column(f"v{i}", sa.String(256), nullable=True)
          for i in range(6)],
    )
    op.create_index("idx_casbin_ptype", "casbin_rule", ["ptype"])
    conn = op.get_bind()
    # quota_*/is_active are Python-side default= only (no server_default) —
    # a raw INSERT MUST supply them or the NOT NULL constraint fails.
    conn.execute(sa.text(
        "INSERT INTO tenants (id, slug, display_name, quota_bytes_month, "
        "quota_concurrent, quota_storage_gb, is_active) "
        "VALUES (1, 'default', 'Default Tenant', 0, 10, 1024, true) "
        "ON CONFLICT (id) DO NOTHING"))
    conn.execute(sa.text(
        "INSERT INTO quota_snapshots (tenant_id) "
        "SELECT id FROM tenants ON CONFLICT (tenant_id) DO NOTHING"))


def downgrade() -> None:
    op.drop_index("idx_casbin_ptype", "casbin_rule")
    op.drop_table("casbin_rule")
    op.drop_table("quota_snapshots")
    op.drop_index("idx_usage_tenant_metric_time", "usage_records")
    op.drop_table("usage_records")
