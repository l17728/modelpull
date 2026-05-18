"""p3sp2 multi source

Revision ID: bb1dd2c45a12
Revises: a4bed702cdb3
Create Date: 2026-05-19 01:22:07.324751

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'bb1dd2c45a12'
down_revision: Union[str, None] = 'a4bed702cdb3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("download_tasks", sa.Column(
        "source_strategy", sa.String(32), nullable=False,
        server_default="auto_balance"))
    op.add_column("download_tasks", sa.Column(
        "source_blacklist", postgresql.JSONB(), nullable=False,
        server_default="[]"))
    op.add_column("download_tasks", sa.Column(
        "trust_non_hf_sha256", sa.Boolean(), nullable=False,
        server_default=sa.false()))
    op.add_column("file_subtasks", sa.Column(
        "source_id", sa.String(32), nullable=True))
    op.add_column("file_subtasks", sa.Column(
        "is_chunked", sa.Boolean(), nullable=False,
        server_default=sa.false()))
    op.create_table(
        "subtask_chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("subtask_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("file_subtasks.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("byte_start", sa.BigInteger(), nullable=False),
        sa.Column("byte_end", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sha256_partial", sa.String(64), nullable=True),
        sa.Column("bytes_done", sa.BigInteger(), nullable=False,
                  server_default="0"),
        sa.UniqueConstraint("subtask_id", "chunk_index"),
    )
    op.create_index("idx_chunk_sub_status", "subtask_chunks",
                    ["subtask_id", "status"])
    op.create_table(
        "source_speed_samples",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("executor_id", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(32), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("bytes_per_sec", sa.Float(), nullable=False),
        sa.Column("sample_size", sa.BigInteger(), nullable=False),
        sa.Column("is_active_probe", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )
    op.create_index("idx_speed_recent", "source_speed_samples",
                    ["executor_id", "source_id", "measured_at"])
    op.create_table(
        "source_blacklist",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("source_id", sa.String(32), nullable=False),
        sa.Column("repo_id", sa.String(256), nullable=True),
        sa.Column("filename", sa.String(512), nullable=True),
        sa.Column("until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_blacklist_lookup", "source_blacklist",
                    ["source_id", "repo_id", "until"])


def downgrade() -> None:
    op.drop_index("idx_blacklist_lookup", "source_blacklist")
    op.drop_table("source_blacklist")
    op.drop_index("idx_speed_recent", "source_speed_samples")
    op.drop_table("source_speed_samples")
    op.drop_index("idx_chunk_sub_status", "subtask_chunks")
    op.drop_table("subtask_chunks")
    op.drop_column("file_subtasks", "is_chunked")
    op.drop_column("file_subtasks", "source_id")
    op.drop_column("download_tasks", "trust_non_hf_sha256")
    op.drop_column("download_tasks", "source_blacklist")
    op.drop_column("download_tasks", "source_strategy")
