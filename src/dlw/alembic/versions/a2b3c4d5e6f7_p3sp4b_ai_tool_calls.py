"""p3sp4b ai tool calls

Revision ID: a2b3c4d5e6f7
Revises: 9a1b2c3d4e5f
Create Date: 2026-05-21
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a2b3c4d5e6f7"
down_revision: str | None = "9a1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_tool_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ai_conversations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ai_messages.id", ondelete="CASCADE"),
                  nullable=True),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("proposed_input", postgresql.JSONB(), nullable=False),
        sa.Column("final_input", postgresql.JSONB(), nullable=True),
        sa.Column("output", postgresql.JSONB(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("requires_confirmation", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("confirmed_by_user_id", sa.BigInteger(),
                  sa.ForeignKey("users.id"), nullable=True),
        sa.Column("confirmation_decision", sa.String(16), nullable=True),
        sa.Column("confirmation_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_ai_tool_conv", "ai_tool_calls", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("idx_ai_tool_conv", table_name="ai_tool_calls")
    op.drop_table("ai_tool_calls")
