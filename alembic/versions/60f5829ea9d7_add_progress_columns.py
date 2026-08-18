"""add_progress_columns

Revision ID: 60f5829ea9d7
Revises: 59b5b7474128
Create Date: 2026-08-18 22:08:38.716665

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '60f5829ea9d7'
down_revision: Union[str, Sequence[str], None] = '59b5b7474128'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("recordings", sa.Column("progress", sa.Double(), nullable=True))
    op.add_column(
        "recordings",
        sa.Column("stage_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # 진행 중인 작업의 진행률과 단계 시작 시각이 사라진다 (완료된 데이터에는 영향 없음)
    op.drop_column("recordings", "stage_started_at")
    op.drop_column("recordings", "progress")
