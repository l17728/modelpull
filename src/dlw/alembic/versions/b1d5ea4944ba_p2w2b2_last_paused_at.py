"""p2w2b2 last_paused_at

Revision ID: b1d5ea4944ba
Revises: 5cfd4bb519f6
Create Date: 2026-05-14 09:18:42.121625

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1d5ea4944ba'
down_revision: Union[str, None] = '5cfd4bb519f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "file_subtasks",
        sa.Column("last_paused_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("file_subtasks", "last_paused_at")
