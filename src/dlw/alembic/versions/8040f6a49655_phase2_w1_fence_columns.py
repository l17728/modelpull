"""phase2 w1 fence columns

Revision ID: 8040f6a49655
Revises: 5a729be99dc0
Create Date: 2026-05-11 17:14:05.210151

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8040f6a49655'
down_revision: Union[str, None] = '5a729be99dc0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('executors',
        sa.Column('epoch', sa.BigInteger(), nullable=False, server_default='0'))
    # W6-H: guard against future seed/fixtures that omit epoch and silently get 0
    # then pass a "0" header that matches. Default-0 is fine for PRE-MIGRATION
    # rows (first /join bumps them to 1); the check just blocks negative values.
    op.create_check_constraint(
        "ck_executors_epoch_nonnegative",
        "executors",
        "epoch >= 0",
    )
    op.add_column('file_subtasks',
        sa.Column('multipart_started_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('file_subtasks',
        sa.Column('assigned_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('file_subtasks',
        sa.Column('last_heartbeat_seen_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('file_subtasks', 'last_heartbeat_seen_at')
    op.drop_column('file_subtasks', 'assigned_at')
    op.drop_column('file_subtasks', 'multipart_started_at')
    op.drop_constraint("ck_executors_epoch_nonnegative", "executors", type_="check")
    op.drop_column('executors', 'epoch')
