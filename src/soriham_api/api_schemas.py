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


class ShareStateOut(BaseModel):
    """이 녹음이 밖으로 얼마나 열려 있는지. 공유를 관리할 수 있는 사람에게만 채운다."""

    user_count: int
    link_count: int


class RecordingDetail(RecordingSummary):
    # 화면이 편집 어포던스를 그릴지. 콘솔이 역할 산술을 다시 하면 두 규칙이 어긋난다
    can_edit: bool = False
    # 공유를 관리할 권한이 없으면 비어 있다
    share_state: ShareStateOut | None = None
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


class ShareIn(BaseModel):
    email: str
    permission: str = "view"


class ShareOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str | None
    permission: str
    # 아직 가입하지 않은 이메일. 승인되면 계정에 이어진다
    pending: bool


class ShareLinkIn(BaseModel):
    label: str | None = None
    password: str | None = None
    allow_audio: bool = True
    # 화자 이름은 손으로 넣은 실명이라 노출을 따로 고른다
    allow_speaker_names: bool = True
    # None이면 무기한
    expires_in_days: int | None = 30


class ShareLinkOut(BaseModel):
    id: uuid.UUID
    label: str | None
    has_password: bool
    allow_audio: bool
    allow_speaker_names: bool
    expires_at: datetime | None
    view_count: int
    last_viewed_at: datetime | None
    created_at: datetime


class IssuedShareLinkOut(ShareLinkOut):
    # 원문 토큰은 발급 응답에만 실린다. 저장하는 것은 해시뿐이다
    token: str


class SharePanelOut(BaseModel):
    """공유 화면 하나가 쓰는 값 전부. 세 갈래를 따로 부르면 화면이 어긋난다."""

    users: list[ShareOut]
    links: list[ShareLinkOut]
    workspace_name: str


class SharedRecordingOut(BaseModel):
    """링크로 여는 응답. 파일명·오류·엔진 메타는 담지 않는다.

    파일명은 그 자체가 내용을 말한다("20260817_인사평가.m4a"). 오류와 엔진 메타는
    바깥 사람이 볼 이유가 없다.
    """

    title: str | None
    summary: str | None
    recorded_at: datetime | None
    duration_sec: float | None
    status: str
    language: str | None
    tags: list[TagOut]
    progress: float | None = None
    eta_sec: float | None = None
    allow_audio: bool
    speaker_names: dict[str, str]
    segments: list[SegmentOut]


class UnlockIn(BaseModel):
    password: str
