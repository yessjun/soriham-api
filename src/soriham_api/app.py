"""FastAPI 앱: 콘솔이 쓰는 REST API."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

import soriham_api
from soriham_api.api_schemas import (
    RecordingDetail,
    RecordingList,
    RecordingSummary,
    SearchHit,
    SearchResult,
    SegmentOut,
    SpeakerNameIn,
    Stats,
    StatusCount,
    TagIn,
    TagOut,
    TitleIn,
)
from soriham_api.config import Settings, load_settings
from soriham_api.db import make_session_factory
from soriham_api.models import JobLog, Recording, SpeakerName, Tag

CHUNK_SIZE = 1024 * 256


def create_app(
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    cfg = settings or load_settings()
    factory = session_factory or make_session_factory(cfg)

    app = FastAPI(title="soriham-api", version=soriham_api.__version__)
    # LAN 내 콘솔(dev 서버 포함)에서 호출 — 인증 없는 사설망 전제
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    def db() -> Iterator[Session]:
        with factory() as session:
            yield session

    def get_recording(session: Session, public_id: uuid.UUID) -> Recording:
        recording = session.scalar(
            select(Recording)
            .where(Recording.public_id == public_id)
            .options(selectinload(Recording.tags))
        )
        if recording is None:
            raise HTTPException(404, "녹음이 없습니다")
        return recording

    @app.get("/api/recordings", response_model=RecordingList)
    def list_recordings(
        session: Session = Depends(db),
        q: str | None = None,
        status: str | None = None,
        tag: uuid.UUID | None = None,
        limit: int = Query(50, le=200),
        offset: int = 0,
    ) -> RecordingList:
        stmt = select(Recording).options(selectinload(Recording.tags))
        stmt = _apply_filters(stmt, q=q, status=status, tag=tag)
        total = session.scalar(
            select(func.count()).select_from(
                _apply_filters(select(Recording), q=q, status=status, tag=tag).subquery()
            )
        )
        rows = session.scalars(
            stmt.order_by(Recording.recorded_at.desc().nulls_last(), Recording.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return RecordingList(items=[_summary(r) for r in rows], total=total or 0)

    @app.get("/api/recordings/{public_id}", response_model=RecordingDetail)
    def recording_detail(public_id: uuid.UUID, session: Session = Depends(db)) -> RecordingDetail:
        recording = session.scalar(
            select(Recording)
            .where(Recording.public_id == public_id)
            .options(
                selectinload(Recording.tags),
                selectinload(Recording.segments),
                selectinload(Recording.speaker_names),
            )
        )
        if recording is None:
            raise HTTPException(404, "녹음이 없습니다")
        return RecordingDetail(
            **_summary(recording).model_dump(),
            error=recording.error,
            stt_meta=recording.stt_meta,
            speaker_names={n.speaker_key: n.display_name for n in recording.speaker_names},
            segments=[
                SegmentOut(
                    idx=s.idx,
                    start_sec=s.start_sec,
                    end_sec=s.end_sec,
                    speaker_key=s.speaker_key,
                    text=s.text,
                )
                for s in recording.segments
            ],
        )

    @app.patch("/api/recordings/{public_id}", response_model=RecordingSummary)
    def update_title(
        public_id: uuid.UUID, body: TitleIn, session: Session = Depends(db)
    ) -> RecordingSummary:
        recording = get_recording(session, public_id)
        recording.title = body.title.strip() or None
        session.commit()
        return _summary(recording)

    @app.put("/api/recordings/{public_id}/speakers/{speaker_key}")
    def rename_speaker(
        public_id: uuid.UUID,
        speaker_key: str,
        body: SpeakerNameIn,
        session: Session = Depends(db),
    ) -> dict[str, str]:
        recording = get_recording(session, public_id)
        row = session.scalar(
            select(SpeakerName).where(
                SpeakerName.recording_id == recording.id,
                SpeakerName.speaker_key == speaker_key,
            )
        )
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "이름이 비어 있습니다")
        if row is None:
            session.add(
                SpeakerName(recording_id=recording.id, speaker_key=speaker_key, display_name=name)
            )
        else:
            row.display_name = name
        session.commit()
        return {speaker_key: name}

    @app.get("/api/recordings/{public_id}/audio")
    def stream_audio(
        public_id: uuid.UUID, request: Request, session: Session = Depends(db)
    ) -> Response:
        recording = get_recording(session, public_id)
        path = Path(recording.path)
        if not path.is_file():
            raise HTTPException(404, "오디오 파일이 없습니다 (드라이브 오프라인일 수 있음)")
        return _range_response(path, request.headers.get("range"))

    @app.post("/api/recordings/{public_id}/tags", response_model=list[TagOut])
    def add_tag(public_id: uuid.UUID, body: TagIn, session: Session = Depends(db)) -> list[TagOut]:
        recording = get_recording(session, public_id)
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "태그 이름이 비어 있습니다")
        tag = session.scalar(select(Tag).where(Tag.name == name))
        if tag is None:
            tag = Tag(name=name)
            session.add(tag)
            session.flush()
        if tag not in recording.tags:
            recording.tags.append(tag)
        session.commit()
        return [TagOut(id=t.public_id, name=t.name) for t in recording.tags]

    @app.delete("/api/recordings/{public_id}/tags/{tag_public_id}")
    def remove_tag(
        public_id: uuid.UUID, tag_public_id: uuid.UUID, session: Session = Depends(db)
    ) -> list[TagOut]:
        recording = get_recording(session, public_id)
        recording.tags = [t for t in recording.tags if t.public_id != tag_public_id]
        session.commit()
        return [TagOut(id=t.public_id, name=t.name) for t in recording.tags]

    @app.get("/api/tags", response_model=list[TagOut])
    def list_tags(session: Session = Depends(db)) -> list[TagOut]:
        return [
            TagOut(id=t.public_id, name=t.name)
            for t in session.scalars(select(Tag).order_by(Tag.name))
        ]

    @app.get("/api/search", response_model=SearchResult)
    def search(
        q: str = Query(min_length=1),
        limit: int = Query(50, le=200),
        session: Session = Depends(db),
    ) -> SearchResult:
        from soriham_api.models import Segment

        pattern = f"%{q}%"
        hits: list[SearchHit] = []
        # 세그먼트 본문 매칭 (녹음 최신순, 세그먼트 순서대로)
        seg_rows = session.execute(
            select(Segment, Recording)
            .join(Recording, Segment.recording_id == Recording.id)
            .where(Segment.text.ilike(pattern))
            .order_by(Recording.recorded_at.desc().nulls_last(), Recording.id.desc(), Segment.idx)
            .limit(limit)
        ).all()
        for seg, rec in seg_rows:
            hits.append(
                SearchHit(
                    recording=_summary(rec),
                    segment=SegmentOut(
                        idx=seg.idx,
                        start_sec=seg.start_sec,
                        end_sec=seg.end_sec,
                        speaker_key=seg.speaker_key,
                        text=seg.text,
                    ),
                )
            )
        # 파일명·제목·요약 매칭
        meta_rows = session.scalars(
            select(Recording)
            .options(selectinload(Recording.tags))
            .where(
                or_(
                    Recording.filename.ilike(pattern),
                    Recording.title.ilike(pattern),
                    Recording.summary.ilike(pattern),
                )
            )
            .order_by(Recording.recorded_at.desc().nulls_last(), Recording.id.desc())
            .limit(limit)
        ).all()
        for rec in meta_rows:
            hits.append(SearchHit(recording=_summary(rec), segment=None))
        return SearchResult(hits=hits[:limit])

    @app.get("/api/stats", response_model=Stats)
    def stats(session: Session = Depends(db)) -> Stats:
        rows = session.execute(
            select(
                Recording.status,
                func.count(),
                func.coalesce(func.sum(Recording.duration_sec), 0.0),
            ).group_by(Recording.status)
        ).all()
        by_status = [StatusCount(status=s, count=c, audio_sec=float(a)) for s, c, a in rows]
        total_audio = sum(x.audio_sec for x in by_status if x.status != "duplicate")
        done_audio = sum(x.audio_sec for x in by_status if x.status == "done")

        # 최근 전사 실측 20건으로 처리 배속 추정
        recent = session.execute(
            select(JobLog.audio_sec, JobLog.elapsed_sec)
            .where(
                JobLog.stage == "transcribe",
                JobLog.status == "done",
                JobLog.audio_sec.is_not(None),
                JobLog.elapsed_sec.is_not(None),
            )
            .order_by(JobLog.id.desc())
            .limit(20)
        ).all()
        speed_ratio = None
        eta_sec = None
        audio_sum = sum(a for a, _ in recent if a)
        if audio_sum > 0:
            speed_ratio = sum(e for _, e in recent) / audio_sum
            pending_audio = sum(
                x.audio_sec for x in by_status if x.status in ("pending", "transcribing")
            )
            eta_sec = pending_audio * speed_ratio

        recent_errors = session.scalars(
            select(Recording)
            .options(selectinload(Recording.tags))
            .where(Recording.status == "error")
            .order_by(Recording.updated_at.desc())
            .limit(10)
        ).all()
        return Stats(
            by_status=by_status,
            done_ratio=(done_audio / total_audio) if total_audio else 0.0,
            speed_ratio=speed_ratio,
            eta_sec=eta_sec,
            recent_errors=[_summary(r) for r in recent_errors],
        )

    return app


def _apply_filters(
    stmt: Select, *, q: str | None, status: str | None, tag: uuid.UUID | None
) -> Select:
    from soriham_api.models import RecordingTag, Segment

    if status:
        stmt = stmt.where(Recording.status == status)
    if tag:
        stmt = (
            stmt.join(RecordingTag, RecordingTag.recording_id == Recording.id)
            .join(Tag, Tag.id == RecordingTag.tag_id)
            .where(Tag.public_id == tag)
        )
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(
                Recording.filename.ilike(pattern),
                Recording.title.ilike(pattern),
                Recording.summary.ilike(pattern),
                Recording.id.in_(select(Segment.recording_id).where(Segment.text.ilike(pattern))),
            )
        )
    return stmt


def _summary(recording: Recording) -> RecordingSummary:
    return RecordingSummary(
        id=recording.public_id,
        filename=recording.filename,
        title=recording.title,
        summary=recording.summary,
        recorded_at=recording.recorded_at,
        duration_sec=recording.duration_sec,
        status=recording.status,
        language=recording.language,
        tags=[TagOut(id=t.public_id, name=t.name) for t in recording.tags],
    )


def _range_response(path: Path, range_header: str | None) -> Response:
    size = path.stat().st_size
    media_type = _media_type(path)
    if not range_header:
        return StreamingResponse(
            _iter_file(path, 0, size - 1),
            media_type=media_type,
            headers={"accept-ranges": "bytes", "content-length": str(size)},
        )
    try:
        unit, _, spec = range_header.partition("=")
        start_s, _, end_s = spec.strip().partition("-")
        if unit.strip().lower() != "bytes":
            raise ValueError
        if start_s:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        else:
            # suffix range: 마지막 N바이트
            start = max(0, size - int(end_s))
            end = size - 1
        if start > end or start >= size:
            raise ValueError
    except ValueError:
        return Response(status_code=416, headers={"content-range": f"bytes */{size}"})
    end = min(end, size - 1)
    return StreamingResponse(
        _iter_file(path, start, end),
        status_code=206,
        media_type=media_type,
        headers={
            "accept-ranges": "bytes",
            "content-range": f"bytes {start}-{end}/{size}",
            "content-length": str(end - start + 1),
        },
    )


def _iter_file(path: Path, start: int, end: int) -> Iterator[bytes]:
    remaining = end - start + 1
    with path.open("rb") as f:
        f.seek(start)
        while remaining > 0:
            chunk = f.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _media_type(path: Path) -> str:
    return {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".wma": "audio/x-ms-wma",
        ".amr": "audio/amr",
    }.get(path.suffix.lower(), "application/octet-stream")
