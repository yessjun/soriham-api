"""add_runner_checkpoint

Revision ID: 356e1ddfca3c
Revises: a1c7f3d20b64
Create Date: 2026-10-03 00:51:47.617298

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '356e1ddfca3c'
down_revision: Union[str, Sequence[str], None] = 'a1c7f3d20b64'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("recordings", sa.Column("runner_request_id", sa.UUID(), nullable=True))
    op.add_column("recordings", sa.Column("runner_started_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # 진행 중인 러너 요청의 재개 정보가 사라진다
    op.drop_column("recordings", "runner_started_at")
    op.drop_column("recordings", "runner_request_id")
