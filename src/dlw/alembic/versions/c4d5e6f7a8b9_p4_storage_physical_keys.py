"""p4 storage physical keys

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-05-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "storage_physical_keys",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.BigInteger(),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("storage_id", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "storage_id", "storage_key",
                            name="uq_phys_key_tenant_storage_key"),
    )
    op.create_index("idx_phys_key_gc", "storage_physical_keys",
                    ["tenant_id", "storage_id", "sha256"])


def downgrade() -> None:
    op.drop_index("idx_phys_key_gc", table_name="storage_physical_keys")
    op.drop_table("storage_physical_keys")
