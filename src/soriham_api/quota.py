"""워크스페이스 사용량 한도.

실제 비용은 GPU 전사 시간이고 저장 용량이 그다음이다. 한도는 워크스페이스에 붙고
비워 두면 무제한이다. 소유자의 스캔 워크스페이스가 그 경우다.

전사 시간은 처리 기록의 최근 30일 합으로 센다. 기록이 workspace_id를 직접 참조해서
녹음을 지워도 남는다. 올리고 전사하고 지우기를 반복해도 한도가 돌아오지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import JobLog, Recording, Workspace

WINDOW_DAYS = 30
# 사라진 원본과 중복은 자리를 차지하지 않는다
STORED_STATUSES_EXCLUDED = ("missing", "duplicate")


@dataclass(frozen=True)
class Usage:
    used_minutes: float
    quota_minutes: int | None
    used_bytes: int
    quota_bytes: int | None
    window_days: int = WINDOW_DAYS

    @property
    def minutes_left(self) -> float | None:
        if self.quota_minutes is None:
            return None
        return max(0.0, self.quota_minutes - self.used_minutes)

    @property
    def bytes_left(self) -> int | None:
        if self.quota_bytes is None:
            return None
        return max(0, self.quota_bytes - self.used_bytes)


class QuotaExceeded(Exception):
    """한도를 넘었다. 메시지는 사용자에게 그대로 보여진다."""


def measure(db: Session, workspace: Workspace, *, now: datetime | None = None) -> Usage:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=WINDOW_DAYS)
    seconds = db.scalar(
        select(func.coalesce(func.sum(JobLog.audio_sec), 0.0)).where(
            JobLog.workspace_id == workspace.id,
            JobLog.stage == "transcribe",
            JobLog.status == "done",
            JobLog.started_at >= since,
        )
    )
    stored = db.scalar(
        select(func.coalesce(func.sum(Recording.size_bytes), 0)).where(
            Recording.workspace_id == workspace.id,
            Recording.status.not_in(STORED_STATUSES_EXCLUDED),
        )
    )
    return Usage(
        used_minutes=float(seconds or 0.0) / 60.0,
        quota_minutes=workspace.quota_minutes,
        used_bytes=int(stored or 0),
        quota_bytes=workspace.quota_bytes,
    )


def check_storage(usage: Usage, incoming_bytes: int) -> None:
    """저장 용량 한도. 파일을 자리에 앉히기 전에 본다."""
    left = usage.bytes_left
    if left is None or incoming_bytes <= left:
        return
    raise QuotaExceeded(
        f"저장 공간이 모자랍니다 (남은 용량 {_gb(left)}, 올리려는 파일 {_gb(incoming_bytes)}). "
        "필요 없는 녹음을 지우거나 한도를 올려 주세요"
    )


def check_minutes(usage: Usage, duration_sec: float | None) -> None:
    """전사 시간 한도.

    한도가 없으면 길이를 몰라도 상관없다. 무제한 워크스페이스에서 길이를 못 읽었다고
    막으면, 소유자의 백로그에서 ffprobe가 실패하는 파일이 통째로 거절된다.

    한도가 있는데 길이를 모르면 거절한다. 모르면 한도를 강제할 방법이 없고, 합계가
    빈 값을 건너뛰므로 영원히 공짜로 전사된다.
    """
    left = usage.minutes_left
    if left is None:
        return
    if duration_sec is None:
        raise QuotaExceeded(
            "오디오 길이를 읽지 못해 받을 수 없습니다. 다른 형식으로 변환해 다시 올려 주세요"
        )
    need = duration_sec / 60.0
    if need > left:
        raise QuotaExceeded(
            f"전사 시간이 모자랍니다 (남은 시간 {left:.0f}분, 이 녹음 {need:.0f}분). "
            f"{WINDOW_DAYS}일이 지나면 회복되거나 한도를 올려야 합니다"
        )


def allows_transcription(db: Session, recording: Recording, *, now: datetime | None = None) -> bool:
    """워커가 러너에 보내기 직전에 하는 한도 검사.

    업로드 시점 검사를 스캔 유입분이 우회하고, 동시 업로드는 검사와 소비 사이에서
    경합한다. 단일 GPU라 여기가 직렬화 지점이고, 그래서 여기가 권위 있는 자리다.
    """
    workspace = db.get(Workspace, recording.workspace_id)
    if workspace is None:
        return True
    usage = measure(db, workspace, now=now)
    try:
        check_minutes(usage, recording.duration_sec)
    except QuotaExceeded:
        return False
    return True


def _gb(value: int) -> str:
    gb = value / (1024 * 1024 * 1024)
    if gb >= 1:
        return f"{gb:.1f}GB"
    mb = value / (1024 * 1024)
    return f"{mb:.0f}MB"
