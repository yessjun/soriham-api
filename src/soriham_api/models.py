"""DB 모델. 식별자 규약: 내부 PK는 서버 밖 비노출, 외부에는 public_id(uuid)만."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# 파일 단위 상태머신 (용어집 기준 + duplicate)
RECORDING_STATUSES = (
    "pending",
    "transcribing",
    "diarizing",
    # enriching은 "요약을 기다리는 중", summarizing은 "지금 워커가 붙어 있는 중"이다.
    # 둘을 한 값으로 겸하면 처리 중인 녹음을 다른 워커가 집어 LLM을 두 번 부른다
    "enriching",
    "summarizing",
    "done",
    "error",
    "missing",
    "duplicate",
    # 사용량 한도를 넘어 전사를 시작하지 않은 상태. 고장이 아니라 기간이 지나거나
    # 한도가 오르면 풀린다
    "quota_blocked",
)
# 녹음이 들어온 경로. 업로드본은 서비스가 지울 수 있고 스캔본은 소유자의 원본이라 두 곳의
# 처리가 갈린다 (삭제, 사라진 파일 스윕)
RECORDING_SOURCES = ("upload", "scan")

USER_STATUSES = ("pending", "active", "rejected", "disabled")
WORKSPACE_KINDS = ("personal", "team")
WORKSPACE_ROLES = ("owner", "admin", "member", "viewer")
SHARE_PERMISSIONS = ("view", "edit")


def _in_check(column: str, values: tuple[str, ...]) -> str:
    return "{} in ({})".format(column, ", ".join(f"'{v}'" for v in values))


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        onupdate=text("now()"),
        nullable=False,
    )


class User(TimestampMixin, Base):
    """서비스 사용자. 가입은 열려 있고 쓸 수 있는지는 `status`가 정한다(승인제)."""

    __tablename__ = "users"
    __table_args__ = (
        # 정규화를 잊으면 INSERT에서 터지게 한다 — 조용히 중복 계정이 생기는 것보다 낫다
        CheckConstraint("email = lower(email)", name="users_email_lower_check"),
        CheckConstraint(_in_check("status", USER_STATUSES), name="users_status_check"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), server_default=text("gen_random_uuid()"), unique=True
    )
    email: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")
    # 신청자가 남기는 "어떤 용도로 쓰려는지" — 승인 판단의 유일한 재료다 (메일이 없다)
    signup_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 가입 승인과 워크스페이스 배정을 하는 운영자. 워크스페이스 역할과는 별개다
    is_service_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    default_workspace_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("workspaces.id", ondelete="SET NULL")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Workspace(TimestampMixin, Base):
    """테넌시 경계. 녹음은 정확히 하나에 속하고 사용량 한도가 여기 붙는다."""

    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(_in_check("kind", WORKSPACE_KINDS), name="workspaces_kind_check"),
        CheckConstraint("slug = lower(slug)", name="workspaces_slug_lower_check"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), server_default=text("gen_random_uuid()"), unique=True
    )
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, default="team", server_default="team")
    # 큐 라운드로빈용 — 이 워크스페이스가 마지막으로 일감을 가져간 시각.
    # NULL(한 번도 안 잡힘)이 맨 앞이라 새로 승인된 사람의 첫 업로드가 바로 돈다
    last_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # 사용량 한도. NULL은 무제한 — 소유자의 스캔 워크스페이스가 그렇다
    quota_minutes: Mapped[int | None] = mapped_column(Integer)
    quota_bytes: Mapped[int | None] = mapped_column(BigInteger)


class WorkspaceMember(TimestampMixin, Base):
    __tablename__ = "workspace_members"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_members_ws_user"),
        CheckConstraint(_in_check("role", WORKSPACE_ROLES), name="workspace_members_role_check"),
        # 소유자를 별도 컬럼으로 두지 않고 여기서 하나만 허용한다. 표현이 둘이면
        # 언젠가 갈라진다
        Index(
            "uq_workspace_members_single_owner",
            "workspace_id",
            unique=True,
            postgresql_where=text("role = 'owner'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(Text, default="member", server_default="member")


class UserSession(Base):
    """로그인 세션. 이름이 `sessions`가 아닌 이유는 sqlalchemy의 Session과 겹쳐서다."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), server_default=text("gen_random_uuid()"), unique=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # 원문 토큰은 저장하지 않는다 — 백업이나 로그가 새도 살아있는 세션이 되지 않게
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    csrf_token: Mapped[str] = mapped_column(Text)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    # 유휴 만료. 요청이 있을 때마다 절대 만료 한도 안에서 뒤로 민다
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(Text)


class Invite(TimestampMixin, Base):
    """워크스페이스 구성원으로 부르는 토큰. 가입 자격과는 무관하다 (가입은 승인제)."""

    __tablename__ = "invites"
    __table_args__ = (
        CheckConstraint("email is null or email = lower(email)", name="invites_email_lower_check"),
        CheckConstraint(_in_check("role", WORKSPACE_ROLES), name="invites_role_check"),
        CheckConstraint("uses <= max_uses", name="invites_uses_check"),
        Index("ix_invites_email", "email"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), server_default=text("gen_random_uuid()"), unique=True
    )
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    # 아직 가입하지 않은 사람에게도 걸어둘 수 있다. 승인될 때 멤버십으로 승격한다
    email: Mapped[str | None] = mapped_column(Text)
    workspace_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(Text, default="member", server_default="member")
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_uses: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    uses: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RecordingShare(TimestampMixin, Base):
    """녹음 하나를 워크스페이스 밖의 특정 사람에게 여는 권한."""

    __tablename__ = "recording_shares"
    __table_args__ = (
        # 가입한 사람(user_id) 또는 아직 아닌 사람(invite_email) 중 정확히 하나
        CheckConstraint(
            "(user_id is null) <> (invite_email is null)",
            name="recording_shares_target_check",
        ),
        CheckConstraint(
            _in_check("permission", SHARE_PERMISSIONS),
            name="recording_shares_permission_check",
        ),
        CheckConstraint(
            "invite_email is null or invite_email = lower(invite_email)",
            name="recording_shares_email_lower_check",
        ),
        Index(
            "uq_recording_shares_user",
            "recording_id",
            "user_id",
            unique=True,
            postgresql_where=text("user_id is not null"),
        ),
        Index(
            "uq_recording_shares_email",
            "recording_id",
            "invite_email",
            unique=True,
            postgresql_where=text("invite_email is not null"),
        ),
        Index("ix_recording_shares_user_id", "user_id"),
        Index("ix_recording_shares_invite_email", "invite_email"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), server_default=text("gen_random_uuid()"), unique=True
    )
    recording_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    invite_email: Mapped[str | None] = mapped_column(Text)
    permission: Mapped[str] = mapped_column(Text, default="view", server_default="view")
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )


class ShareLink(TimestampMixin, Base):
    """로그인 없이 녹음 하나를 여는 추측 불가 토큰. 열람 전용이다."""

    __tablename__ = "share_links"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), server_default=text("gen_random_uuid()"), unique=True
    )
    recording_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    # 원문 토큰은 발급 응답에 한 번만 실린다
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    label: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str | None] = mapped_column(Text)
    allow_audio: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    # 화자 이름은 소유자가 손으로 넣은 실명이라 노출을 따로 고르게 한다
    allow_speaker_names: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 보안 신호가 아니라 근사치다 — 메신저 미리보기와 백신이 부풀린다
    view_count: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )


class AuthAttempt(Base):
    """로그인·가입 시도 횟수. 비밀번호 해싱보다 먼저 막기 위한 것이다."""

    __tablename__ = "auth_attempts"
    __table_args__ = (
        UniqueConstraint("key", "window_start", name="uq_auth_attempts_key_window"),
        Index("ix_auth_attempts_window_start", "window_start"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Recording(TimestampMixin, Base):
    __tablename__ = "recordings"
    __table_args__ = (
        CheckConstraint(
            _in_check("status", RECORDING_STATUSES),
            name="recordings_status_check",
        ),
        CheckConstraint(_in_check("source", RECORDING_SOURCES), name="recordings_source_check"),
        # 큐 소비용. 마이그레이션에만 있던 인덱스를 메타데이터에도 선언한다 — 선언이
        # 없으면 드리프트 검사가 매번 "지워야 할 인덱스"로 잡는다
        Index("ix_recordings_status_workspace", "status", "workspace_id"),
        Index(
            "ix_recordings_workspace_recorded_at",
            "workspace_id",
            text("recorded_at DESC NULLS LAST"),
            text("id DESC"),
        ),
        # 중복 판정은 워크스페이스 안에서만 한다 — 전역이면 남이 같은 파일을 가졌는지
        # 알아내는 탐침이 된다
        Index("ix_recordings_workspace_partial_hash", "workspace_id", "partial_hash"),
        Index("ix_recordings_workspace_content_hash", "workspace_id", "content_hash"),
        # 한국어 부분 문자열 검색(pg_trgm). 표현식이 아니라 컬럼 인덱스지만
        # 연산자 클래스가 기본과 달라 postgresql_ops를 명시해야 실물과 일치한다
        Index(
            "ix_recordings_filename_trgm",
            "filename",
            postgresql_using="gin",
            postgresql_ops={"filename": "gin_trgm_ops"},
        ),
        Index(
            "ix_recordings_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index(
            "ix_recordings_summary_trgm",
            "summary",
            postgresql_using="gin",
            postgresql_ops={"summary": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), server_default=text("gen_random_uuid()"), unique=True
    )
    workspace_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(Text, default="upload", server_default="upload")
    # 절대경로라 같은 경로는 같은 바이트다 — 전역 유일이 맞다. 업로드 경로가
    # 워크스페이스로 갈려서 교차 충돌은 구조적으로 생기지 않는다
    path: Mapped[str] = mapped_column(Text, unique=True)
    filename: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    # 크기와 앞뒤 1MB로 만든 싼 키. 청크 크기를 바꾸면 기존 값과 비교가 깨지므로
    # 동일성의 정본은 아래 content_hash다
    partial_hash: Mapped[str] = mapped_column(Text)
    # 파일 내용 전체의 sha256. 이름과 경로가 바뀌어도 같은 녹음을 다시 찾는 키이고,
    # 파라미터가 없어 나중에 계산한 값과도 비교된다. 백필 전 행은 비어 있다
    content_hash: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_sec: Mapped[float | None] = mapped_column(Double)
    language: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    stt_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # 진행 중인 단계의 비율(0~1)과 그 단계 시작 시각. 남은 시간 계산에 쓴다
    progress: Mapped[float | None] = mapped_column(Double)
    stage_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duplicate_of_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("recordings.id", ondelete="SET NULL")
    )

    segments: Mapped[list[Segment]] = relationship(
        back_populates="recording", cascade="all, delete-orphan", order_by="Segment.idx"
    )
    speaker_names: Mapped[list[SpeakerName]] = relationship(
        back_populates="recording", cascade="all, delete-orphan"
    )
    tags: Mapped[list[Tag]] = relationship(secondary="recording_tags", back_populates="recordings")


class Segment(Base):
    __tablename__ = "segments"
    __table_args__ = (
        UniqueConstraint("recording_id", "idx"),
        Index(
            "ix_segments_text_trgm",
            "text",
            postgresql_using="gin",
            postgresql_ops={"text": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    recording_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer)
    start_sec: Mapped[float] = mapped_column(Double)
    end_sec: Mapped[float] = mapped_column(Double)
    speaker_key: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    words: Mapped[list[Any] | None] = mapped_column(JSONB)
    # speech | noise — noise는 말이 아닌 구간을 러너가 판정해 표시한 자리다
    kind: Mapped[str] = mapped_column(Text, default="speech", server_default="speech")

    recording: Mapped[Recording] = relationship(back_populates="segments")


class SpeakerName(Base):
    __tablename__ = "speaker_names"
    __table_args__ = (UniqueConstraint("recording_id", "speaker_key"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    recording_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    speaker_key: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)

    recording: Mapped[Recording] = relationship(back_populates="speaker_names")


class Tag(TimestampMixin, Base):
    __tablename__ = "tags"
    # 이름을 전역 유일로 두면 한 워크스페이스가 만든 "인사평가"를 다른 곳이 그대로
    # 물려받고, 태그 목록이 남의 회의 주제를 흘린다
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_tags_workspace_name"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), server_default=text("gen_random_uuid()"), unique=True
    )
    workspace_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(Text)

    recordings: Mapped[list[Recording]] = relationship(
        secondary="recording_tags", back_populates="tags"
    )


class RecordingTag(Base):
    __tablename__ = "recording_tags"

    recording_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("recordings.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )


class JobLog(Base):
    """처리 단계별 소요 실측. 대시보드 남은 시간과 사용량 한도 산정의 근거다.

    녹음을 지워도 남는다. 이 행이 녹음을 따라 사라지면 올리고 전사하고 지우기를
    반복해 사용량 한도를 되돌릴 수 있다. workspace_id를 따로 두고 녹음 참조만 끊는다.
    """

    __tablename__ = "job_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    recording_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("recordings.id", ondelete="SET NULL"), index=True
    )
    stage: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    audio_sec: Mapped[float | None] = mapped_column(Double)
    elapsed_sec: Mapped[float | None] = mapped_column(Double)
    device: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
