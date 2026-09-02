"""add_content_hash

Revision ID: a1c7f3d20b64
Revises: e52dbbaf2130
Create Date: 2026-09-02 20:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c7f3d20b64"
down_revision: str | Sequence[str] | None = "e52dbbaf2130"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 기존 행은 값이 없다. 백필 전까지 부분 해시로 판정하므로 nullable이다
    op.add_column("recordings", sa.Column("content_hash", sa.Text(), nullable=True))
    op.create_index(
        "ix_recordings_workspace_content_hash",
        "recordings",
        ["workspace_id", "content_hash"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_recordings_workspace_content_hash", table_name="recordings")
    op.drop_column("recordings", "content_hash")
