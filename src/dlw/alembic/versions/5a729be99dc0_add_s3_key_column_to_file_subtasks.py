"""add s3_key column to file_subtasks

Revision ID: 5a729be99dc0
Revises: 055a5cb9cf28
Create Date: 2026-05-09 17:47:00.895294

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5a729be99dc0'
down_revision: Union[str, None] = '055a5cb9cf28'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('file_subtasks', sa.Column('s3_key', sa.String(length=1024), nullable=True))


def downgrade() -> None:
    op.drop_column('file_subtasks', 's3_key')
