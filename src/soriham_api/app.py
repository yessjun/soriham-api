"""FastAPI 앱: 콘솔이 쓰는 REST API."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, UploadFile
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
from soriham_api.ingest import (
    AUDIO_EXTENSIONS,
    find_duplicate,
    ingest_file,
    partial_hash,
)
from soriham_api.models import JobLog, Recording, SpeakerName, Tag
from soriham_api.uploads import (
    UploadEmpty,
    UploadTooLarge,
    cleanup_staging,
    finalize,
    safe_filename,
    stage_upload,
)

CHUNK_SIZE = 1024 * 256


def _like_pattern(q: str) -> str:
    """ILIKE 와일드카드(%, _)와 이스케이프 문자를 리터럴로 취급한다."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def create_app(
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    cfg = settings or load_settings()
    factory = session_factory or make_session_factory(cfg)

    app = FastAPI(title="soriham-api", version=soriham_api.__version__)
    # 브라우저 경유 접근을 설정된 콘솔 오리진으로 한정 (사설망이라도 * 개방은
    # 임의 웹페이지의 녹취록 열람을 허용하게 됨)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if cfg.upload_dir is not None:
        # 이전 실행이 중단되며 남긴 미완성 업로드 정리
        cleanup_staging(cfg.upload_dir)
    # 해시 판정부터 커밋까지를 직렬화한다 — 동시 업로드가 같은 경로·같은 내용을
    # 서로 못 보고 지나가면 파일이 덮이거나 중복 거절이 새어 나간다
    upload_lock = threading.Lock()

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

    @app.post("/api/recordings", response_model=RecordingSummary, status_code=201)
    def upload_recording(file: UploadFile, session: Session = Depends(db)) -> RecordingSummary:
        """콘솔에서 올린 오디오를 보관 폴더에 저장하고 큐에 등록한다."""
        if cfg.upload_dir is None:
            raise HTTPException(503, "업로드가 설정돼 있지 않습니다 (UPLOAD_DIR)")
        name = safe_filename(file.filename)
        if name is None:
            raise HTTPException(422, "파일 이름이 없습니다")
        suffix = Path(name).suffix.lower()
        if suffix not in AUDIO_EXTENSIONS:
            raise HTTPException(415, f"지원하지 않는 오디오 형식입니다: {suffix or '확장자 없음'}")

        try:
            staged = stage_upload(file.file, cfg.upload_dir, cfg.max_upload_bytes)
        except UploadTooLarge:
            limit_mb = cfg.max_upload_bytes // (1024 * 1024)
            raise HTTPException(413, f"파일이 너무 큽니다 (최대 {limit_mb}MB)") from None
        except UploadEmpty:
            raise HTTPException(422, "빈 파일입니다") from None

        def registered(path: Path) -> bool:
            return (
                session.scalar(select(Recording.id).where(Recording.path == str(path.resolve())))
                is not None
            )

        with upload_lock:
            dest: Path | None = None
            try:
                digest = partial_hash(staged, staged.stat().st_size)
                original = find_duplicate(session, digest)
                if original is not None:
                    # 같은 내용이 이미 있으면 사본을 남기지 않고 기존 항목으로 돌려보낸다
                    raise HTTPException(
                        409,
                        {
                            "message": f"이미 등록된 파일입니다: {original.filename}",
                            "recording_id": str(original.public_id),
                        },
                    )
                # 디스크에 없더라도 DB에 등록된 경로는 피한다 — 파일만 지워진 기존
                # 녹음의 자리를 새 파일이 차지하면 그 행이 엉뚱한 오디오를 가리킨다
                dest = finalize(staged, cfg.upload_dir, name, taken=registered)
                recording = ingest_file(session, dest)
                if recording is None:  # registered()가 걸렀어야 하는 경우
                    raise HTTPException(500, "업로드 경로를 정하지 못했습니다")
                session.commit()
            except BaseException:
                session.rollback()
                staged.unlink(missing_ok=True)
                if dest is not None:
                    dest.unlink(missing_ok=True)
                raise
        return _summary(recording)

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
                    kind=s.kind,
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

        pattern = _like_pattern(q)
        # 파일명·제목·요약 매칭을 먼저 배치 — 세그먼트 히트가 limit을 채워
        # 정확한 제목 일치가 밀려나지 않게 한다
        hits: list[SearchHit] = []
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
                        kind=seg.kind,
                    ),
                )
            )
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
        valid = [(a, e) for a, e in recent if a]
        audio_sum = sum(a for a, _ in valid)
        if audio_sum > 0:
            speed_ratio = sum(e for _, e in valid) / audio_sum
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
        pattern = _like_pattern(q)
        stmt = stmt.where(
            or_(
                Recording.filename.ilike(pattern),
                Recording.title.ilike(pattern),
                Recording.summary.ilike(pattern),
                Recording.id.in_(select(Segment.recording_id).where(Segment.text.ilike(pattern))),
            )
        )
    return stmt


def _eta_sec(recording: Recording) -> float | None:
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
        progress=recording.progress,
        eta_sec=_eta_sec(recording),
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
