"""v2.1 Sprint 4 — add replication_jobs table

Tracks "copy storage_object X from its current backend to target backend Y"
work items. Sprint 4 only adds the schema + status field. Sprint 5 will
add the worker that actually streams bytes.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-05-26 (v2.1 sprint 4)
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "replication_jobs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.BigInteger,
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("source_object_id", sa.BigInteger,
                  sa.ForeignKey("storage_objects.id"), nullable=False),
        sa.Column("target_storage_id", sa.BigInteger,
                  sa.ForeignKey("storage_backends.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False,
                  server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bytes_transferred", sa.BigInteger,
                  server_default="0", nullable=False),
        sa.Column("retry_count", sa.Integer,
                  server_default="0", nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', "
            "'cancelled', 'skipped_existing')",
            name="ck_replication_jobs_status"),
        # Prevent the same (object → target) job from being queued twice
        # while one is still active.
        sa.Index(
            "ux_replication_jobs_active_unique",
            "source_object_id", "target_storage_id",
            unique=True,
            postgresql_where=sa.text("status IN ('pending', 'running')")),
        sa.Index("ix_replication_jobs_tenant_status",
                  "tenant_id", "status"),
    )


def downgrade() -> None:
    op.drop_index("ix_replication_jobs_tenant_status",
                   table_name="replication_jobs")
    op.drop_index("ux_replication_jobs_active_unique",
                   table_name="replication_jobs")
    op.drop_table("replication_jobs")
