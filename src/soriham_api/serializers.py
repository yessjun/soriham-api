"""모델 행을 응답 스키마로 옮기는 함수들.

로그인 화면과 공유 링크 화면이 같은 녹음을 서로 다른 모양으로 내보내므로, 옮기는
규칙을 한 곳에 모아 둔다 — 흩어지면 한쪽에만 필드가 늘어난다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .api_schemas import RecordingSummary, SegmentOut, TagOut
from .models import Recording, Segment


def eta_sec(recording: Recording) -> float | None:
    """진행률과 단계 경과 시간으로 남은 시간을 추정한다.

    진행률이 없거나 아직 0이면 추정할 근거가 없으므로 None.
    """
    progress = recording.progress
    started = recording.stage_started_at
    if not progress or started is None or progress >= 1:
        return None
    elapsed = (datetime.now(UTC) - started).total_seconds()
    if elapsed <= 0:
        return None
    return elapsed / progress * (1 - progress)


def tag_out(tag) -> TagOut:
    return TagOut(id=tag.public_id, name=tag.name)


def segment_out(segment: Segment) -> SegmentOut:
    return SegmentOut(
        idx=segment.idx,
        start_sec=segment.start_sec,
        end_sec=segment.end_sec,
        speaker_key=segment.speaker_key,
        text=segment.text,
        kind=segment.kind,
    )


def recording_summary(recording: Recording) -> RecordingSummary:
    return RecordingSummary(
        id=recording.public_id,
        filename=recording.filename,
        title=recording.title,
        summary=recording.summary,
        recorded_at=recording.recorded_at,
        duration_sec=recording.duration_sec,
        status=recording.status,
        language=recording.language,
        tags=[tag_out(t) for t in recording.tags],
        progress=recording.progress,
        eta_sec=eta_sec(recording),
    )
