"""add scheduler hot-path indexes

Revision ID: 055a5cb9cf28
Revises: bce7fa21af9c
Create Date: 2026-05-07 21:27:40.363792

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '055a5cb9cf28'
down_revision: Union[str, None] = 'bce7fa21af9c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Hot path: scheduler.claim_one_subtask
    # SELECT FROM file_subtasks WHERE status='pending' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED
    op.create_index(
        "ix_file_subtasks_pending_created",
        "file_subtasks",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    # Hot path: list_tasks endpoint
    # SELECT FROM download_tasks WHERE tenant_id=? ORDER BY created_at DESC
    op.create_index(
        "ix_download_tasks_tenant_created",
        "download_tasks",
        ["tenant_id", "created_at"],
        postgresql_ops={"created_at": "DESC NULLS LAST"},
    )


def downgrade() -> None:
    op.drop_index("ix_download_tasks_tenant_created", table_name="download_tasks")
    op.drop_index("ix_file_subtasks_pending_created", table_name="file_subtasks")
