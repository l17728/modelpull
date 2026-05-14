"""p2w3a hmac_seed

Revision ID: 6f37b72630ce
Revises: b1d5ea4944ba
Create Date: 2026-05-14 15:23:03.022914

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6f37b72630ce'
down_revision: Union[str, None] = 'b1d5ea4944ba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "executors",
        sa.Column("hmac_seed_encrypted", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("executors", "hmac_seed_encrypted")
