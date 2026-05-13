"""p2w2a state machine

Revision ID: 5cfd4bb519f6
Revises: 8040f6a49655
Create Date: 2026-05-13 18:56:27.553306

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '5cfd4bb519f6'
down_revision: Union[str, None] = '8040f6a49655'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. New column on executors: degraded_recoveries (counter used by state machine).
    op.add_column(
        "executors",
        sa.Column("degraded_recoveries", sa.Integer(), nullable=False, server_default="0"),
    )

    # 2. New table.
    op.create_table(
        "executor_status_history",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "executor_id", sa.String(64),
            sa.ForeignKey("executors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(32), nullable=False),
        sa.Column("to_status",   sa.String(32), nullable=False),
        sa.Column("event",       sa.String(32), nullable=False),
        sa.Column("reason",      sa.String(256), nullable=False),
        sa.Column(
            "transitioned_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "metadata", postgresql.JSONB,
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
    )
    op.create_index(
        "ix_esh_executor_time",
        "executor_status_history",
        ["executor_id", sa.text("transitioned_at DESC")],
    )

    # 3. Data migration: legacy 'unhealthy' → 'faulty'. 'joining' rows stay
    #    valid (joining is in the W2a value domain). 'healthy' rows untouched.
    op.execute("UPDATE executors SET status = 'faulty' WHERE status = 'unhealthy'")

    # 4. Synthetic history row for each migrated row so the audit trail is complete.
    op.execute("""
        INSERT INTO executor_status_history
          (executor_id, from_status, to_status, event, reason, metadata)
        SELECT id, 'unhealthy', 'faulty', 'admin',
               'P2-W2a migration: legacy unhealthy -> faulty',
               '{}'::jsonb
        FROM executors WHERE status = 'faulty'
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE executors SET status = 'unhealthy'
        WHERE status IN ('faulty', 'suspect', 'degraded')
    """)
    op.drop_index("ix_esh_executor_time", table_name="executor_status_history")
    op.drop_table("executor_status_history")
    op.drop_column("executors", "degraded_recoveries")
