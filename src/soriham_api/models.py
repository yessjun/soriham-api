"""DB 모델. 식별자 규약: 내부 PK는 서버 밖 비노출, 외부에는 public_id(uuid)만."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Identity,
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
    "enriching",
    "done",
    "error",
    "missing",
    "duplicate",
)


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


class Recording(TimestampMixin, Base):
    __tablename__ = "recordings"
    __table_args__ = (
        CheckConstraint(
            "status in ({})".format(", ".join(f"'{s}'" for s in RECORDING_STATUSES)),
            name="recordings_status_check",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), server_default=text("gen_random_uuid()"), unique=True
    )
    path: Mapped[str] = mapped_column(Text, unique=True)
    filename: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    partial_hash: Mapped[str] = mapped_column(Text, index=True)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_sec: Mapped[float | None] = mapped_column(Double)
    language: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    stt_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
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
    __table_args__ = (UniqueConstraint("recording_id", "idx"),)

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

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), server_default=text("gen_random_uuid()"), unique=True
    )
    name: Mapped[str] = mapped_column(Text, unique=True)

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
    """처리 단계별 소요 실측 — 대시보드 ETA와 엔진별 비용 계산의 근거."""

    __tablename__ = "job_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    recording_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("recordings.id", ondelete="CASCADE"), index=True
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
