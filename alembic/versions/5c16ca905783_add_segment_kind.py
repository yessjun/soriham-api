"""add_segment_kind

Revision ID: 5c16ca905783
Revises: 60f5829ea9d7
Create Date: 2026-08-18 23:08:00.338128

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5c16ca905783'
down_revision: Union[str, Sequence[str], None] = '60f5829ea9d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "segments",
        sa.Column("kind", sa.Text(), nullable=False, server_default="speech"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # 소음으로 표시된 구간과 발화의 구분이 사라진다
    op.drop_column("segments", "kind")
