"""REST 응답 스키마. 외부 식별자는 public_id(uuid)만 노출한다."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class TagOut(BaseModel):
    id: uuid.UUID
    name: str


class RecordingSummary(BaseModel):
    id: uuid.UUID
    filename: str
    title: str | None
    summary: str | None
    recorded_at: datetime | None
    duration_sec: float | None
    status: str
    language: str | None
    tags: list[TagOut]


class RecordingList(BaseModel):
    items: list[RecordingSummary]
    total: int


class SegmentOut(BaseModel):
    idx: int
    start_sec: float
    end_sec: float
    speaker_key: str | None
    text: str


class RecordingDetail(RecordingSummary):
    error: str | None
    stt_meta: dict[str, Any] | None
    speaker_names: dict[str, str]
    segments: list[SegmentOut]


class SearchHit(BaseModel):
    recording: RecordingSummary
    segment: SegmentOut | None  # None이면 파일명·제목·요약 매칭


class SearchResult(BaseModel):
    hits: list[SearchHit]


class SpeakerNameIn(BaseModel):
    name: str


class TagIn(BaseModel):
    name: str


class TitleIn(BaseModel):
    title: str


class StatusCount(BaseModel):
    status: str
    count: int
    audio_sec: float


class Stats(BaseModel):
    by_status: list[StatusCount]
    done_ratio: float
    # 최근 처리 실측이 있을 때: 처리 배속(오디오 1초당 처리 소요초)과 남은 예상 시간
    speed_ratio: float | None
    eta_sec: float | None
    recent_errors: list[RecordingSummary]
