"""p3sp4a ai copilot

Revision ID: 9a1b2c3d4e5f
Revises: 7636b35e4881
Create Date: 2026-05-21
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9a1b2c3d4e5f"
down_revision: str | None = "7636b35e4881"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("backend", sa.String(32), nullable=False),
        sa.Column("model_name", sa.String(64), nullable=False),
    )
    op.create_index("idx_ai_conv_owner", "ai_conversations",
                    ["owner_user_id", "last_message_at"])
    op.create_index("idx_ai_conv_tenant", "ai_conversations",
                    ["tenant_id", "last_message_at"])
    op.create_table(
        "ai_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ai_conversations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("tokens_input", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("tokens_output", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_ai_msg_conv", "ai_messages",
                    ["conversation_id", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_ai_msg_conv", table_name="ai_messages")
    op.drop_table("ai_messages")
    op.drop_index("idx_ai_conv_tenant", table_name="ai_conversations")
    op.drop_index("idx_ai_conv_owner", table_name="ai_conversations")
    op.drop_table("ai_conversations")
