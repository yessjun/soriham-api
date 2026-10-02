"""track_current_recording_path

Revision ID: ed92879e0eb9
Revises: 356e1ddfca3c
Create Date: 2026-10-03 02:41:17.727731

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'ed92879e0eb9'
down_revision: Union[str, Sequence[str], None] = '356e1ddfca3c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("recordings", sa.Column("path_current", sa.Boolean(), nullable=False,
                                         server_default=sa.true()))
    op.add_column("recordings", sa.Column("file_signature", postgresql.JSONB(), nullable=True))
    op.drop_constraint("recordings_path_key", "recordings", type_="unique")
    op.create_index("uq_recordings_current_path", "recordings", ["path"], unique=True,
                    postgresql_where=sa.text("path_current"))


def downgrade() -> None:
    """Downgrade schema."""
    # 과거 행을 지워서 이전 유일 제약에 맞추지 않는다
    duplicates = op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM recordings GROUP BY path HAVING count(*) > 1)"
    ))
    if duplicates:
        raise RuntimeError("같은 경로의 과거 녹음이 있어 이전 스키마로 되돌릴 수 없습니다")
    op.drop_index("uq_recordings_current_path", table_name="recordings")
    op.create_unique_constraint("recordings_path_key", "recordings", ["path"])
    op.drop_column("recordings", "file_signature")
    op.drop_column("recordings", "path_current")
