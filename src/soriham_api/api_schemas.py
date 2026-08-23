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
    # 진행 중일 때만 채워진다. eta_sec은 저장하지 않고 응답 시점에 계산한다
    progress: float | None = None
    eta_sec: float | None = None


class RecordingList(BaseModel):
    items: list[RecordingSummary]
    total: int


class SegmentOut(BaseModel):
    idx: int
    start_sec: float
    end_sec: float
    speaker_key: str | None
    text: str
    # speech | noise
    kind: str = "speech"


class RecordingDetail(RecordingSummary):
    # 화면이 편집 어포던스를 그릴지. 콘솔이 역할 산술을 다시 하면 두 규칙이 어긋난다
    can_edit: bool = False
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


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str


class WorkspaceRef(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    role: str


class MeOut(BaseModel):
    user: UserOut
    status: str
    workspaces: list[WorkspaceRef]
    default_workspace_id: uuid.UUID | None
    # 화면이 무엇을 그릴지 정하는 값. 콘솔이 역할 산술을 다시 하지 않게 서버가 준다
    capabilities: list[str]
    pending_user_count: int | None = None


class SignupIn(BaseModel):
    email: str
    password: str
    display_name: str
    signup_note: str | None = None


class LoginIn(BaseModel):
    email: str
    password: str


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str


class PendingUserOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    signup_note: str | None
    requested_at: datetime


class MemberOut(BaseModel):
    user: UserOut
    role: str
    joined_at: datetime


class WorkspaceCreateIn(BaseModel):
    name: str
    slug: str


class RoleIn(BaseModel):
    role: str


class UsageOut(BaseModel):
    used_minutes: float
    # 비어 있으면 무제한
    quota_minutes: int | None
    used_bytes: int
    quota_bytes: int | None
    # 롤링 창이라 시작점이 없다. 며칠치를 세는지만 말한다
    window_days: int
