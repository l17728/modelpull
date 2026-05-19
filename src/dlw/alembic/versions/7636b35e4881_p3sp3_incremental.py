"""p3sp3 incremental

Revision ID: 7636b35e4881
Revises: bb1dd2c45a12
Create Date: 2026-05-19 09:40:54.574321

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '7636b35e4881'
down_revision: str | None = 'bb1dd2c45a12'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("file_subtasks", sa.Column(
        "inherit_from_key", sa.String(1024), nullable=True))
    op.create_table(
        "storage_objects",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("storage_id", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("refcount", sa.Integer(), nullable=False,
                  server_default="1"),
        sa.Column("last_referenced_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "storage_id", "sha256"),
    )
    op.create_index("idx_storage_obj_gc", "storage_objects",
                    ["refcount", "created_at"])
    op.create_table(
        "subtask_object_refs",
        sa.Column("subtask_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("file_subtasks.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("object_id", sa.BigInteger(),
                  sa.ForeignKey("storage_objects.id"), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("subtask_object_refs")
    op.drop_index("idx_storage_obj_gc", "storage_objects")
    op.drop_table("storage_objects")
    op.drop_column("file_subtasks", "inherit_from_key")
