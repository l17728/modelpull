"""fu5 phys key last_referenced_at

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-05-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6f7a8b9c0d1"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("storage_physical_keys", sa.Column(
        "last_referenced_at", sa.DateTime(timezone=True),
        server_default=sa.func.now(), nullable=False))
    op.execute(
        "UPDATE storage_physical_keys SET last_referenced_at = created_at")


def downgrade() -> None:
    op.drop_column("storage_physical_keys", "last_referenced_at")
