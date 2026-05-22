"""fu3 phys key executor_id

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-05-22
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("storage_physical_keys",
                  sa.Column("executor_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("storage_physical_keys", "executor_id")
