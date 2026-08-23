"""add_summarizing_status

Revision ID: e52dbbaf2130
Revises: 2e6e9a392f6f
Create Date: 2026-08-23

요약 대기(enriching)와 요약 처리 중(summarizing)을 가른다. 한 값으로 겸하면 워커가
집어 처리 중인 녹음을 다른 워커가 다시 집어 LLM을 두 번 부른다.

상태 목록은 모델에서 가져오지 않고 직접 적는다. 적용된 리비전은 불변이어야 하는데,
모델 상수를 읽으면 나중에 그 상수가 바뀔 때 같은 리비전 id가 DB마다 다른 일을 한다.
"""

from alembic import op

revision = "e52dbbaf2130"
down_revision = "2e6e9a392f6f"
branch_labels = None
depends_on = None

BEFORE = (
    "pending",
    "transcribing",
    "diarizing",
    "enriching",
    "done",
    "error",
    "missing",
    "duplicate",
    "quota_blocked",
)
AFTER = (*BEFORE, "summarizing")


def _set_check(values: tuple[str, ...]) -> None:
    op.drop_constraint("recordings_status_check", "recordings", type_="check")
    op.create_check_constraint(
        "recordings_status_check",
        "recordings",
        "status in ({})".format(", ".join(f"'{s}'" for s in values)),
    )


def upgrade() -> None:
    _set_check(AFTER)


def downgrade() -> None:
    """summarizing으로 남아 있던 녹음은 요약 대기로 돌아간다(산출물은 그대로)."""
    op.execute("update recordings set status = 'enriching' where status = 'summarizing'")
    _set_check(BEFORE)
