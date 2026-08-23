"""FastAPI 앱: 콘솔이 쓰는 REST API."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker

import soriham_api
from soriham_api.api_schemas import (
    RecordingDetail,
    RecordingList,
    RecordingSummary,
    SearchHit,
    SearchResult,
    ShareStateOut,
    SpeakerNameIn,
    Stats,
    StatusCount,
    TagIn,
    TagOut,
    TitleIn,
    UsageOut,
)
from soriham_api.config import Settings, load_settings
from soriham_api.db import make_session_factory
from soriham_api.deps import CSRF_HEADER, WorkspaceContext, build_deps
from soriham_api.ingest import (
    AUDIO_EXTENSIONS,
    find_duplicate,
    ingest_file,
    partial_hash,
    probe_duration,
)
from soriham_api.media import range_response
from soriham_api.models import JobLog, Recording, SpeakerName, Tag, User, Workspace
from soriham_api.permissions import Perm, Principal, resolve_recording_perm
from soriham_api.quota import (
    QuotaExceeded,
    Usage,
    check_minutes,
    check_storage,
    measure,
)
from soriham_api.routes_auth import register as register_auth_routes
from soriham_api.routes_sharing import register as register_sharing_routes
from soriham_api.routes_workspaces import register as register_workspace_routes
from soriham_api.serializers import recording_summary as _summary
from soriham_api.serializers import segment_out
from soriham_api.sharing import share_counts
from soriham_api.storage import (
    AudioUnavailable,
    resolve_audio_path,
    workspace_upload_dir,
)
from soriham_api.tenancy import resolve_tag
from soriham_api.uploads import (
    UploadEmpty,
    UploadTooLarge,
    cleanup_staging,
    finalize,
    safe_filename,
    stage_upload,
)


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

    app = FastAPI(
        title="soriham-api",
        version=soriham_api.__version__,
        # 운영에서는 스키마를 열어 두지 않는다. 라우트 목록 자체가 정찰 재료다
        docs_url="/docs" if cfg.expose_docs else None,
        redoc_url="/redoc" if cfg.expose_docs else None,
        openapi_url="/openapi.json" if cfg.expose_docs else None,
    )
    # 브라우저 경유 접근을 설정된 콘솔 오리진으로 한정 (사설망이라도 * 개방은
    # 임의 웹페이지의 녹취록 열람을 허용하게 됨). 쿠키를 실어 보내므로
    # 자격증명 허용이 필요하고, 그래서 오리진에 *를 쓸 수 없다 — 설정이 막는다
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["content-type", CSRF_HEADER],
        # 플레이어가 Range 응답을 읽으려면 이 둘이 보여야 한다
        expose_headers=["content-range", "accept-ranges"],
    )

    if cfg.upload_dir is not None:
        # 이전 실행이 중단되며 남긴 미완성 업로드 정리
        cleanup_staging(cfg.upload_dir)
    # 해시 판정부터 커밋까지를 직렬화한다 — 동시 업로드가 같은 경로·같은 내용을
    # 서로 못 보고 지나가면 파일이 덮이거나 중복 거절이 새어 나간다
    upload_lock = threading.Lock()
    deps = build_deps(cfg, factory, upload_lock)
    db = deps.db
    # 권한 등급마다 한 번씩만 만든다. 경로 파라미터로 녹음을 얻는 길은 이 둘뿐이다
    viewable = deps.recording_at(Perm.VIEW)
    editable = deps.recording_at(Perm.EDIT)
    manageable = deps.recording_at(Perm.MANAGE)
    register_auth_routes(app, deps)
    register_sharing_routes(app, deps)
    register_workspace_routes(app, deps)

    @app.get("/api/workspaces/{workspace_id}/recordings", response_model=RecordingList)
    def list_recordings(
        ctx: WorkspaceContext = Depends(deps.workspace_ctx),
        session: Session = Depends(db),
        q: str | None = None,
        status: str | None = None,
        tag: uuid.UUID | None = None,
        limit: int = Query(50, le=200),
        offset: int = 0,
    ) -> RecordingList:
        ws = ctx.workspace.id
        stmt = select(Recording).options(selectinload(Recording.tags))
        stmt = _apply_filters(stmt, q=q, status=status, tag=tag, workspace_id=ws)
        total = session.scalar(
            select(func.count()).select_from(
                _apply_filters(
                    select(Recording), q=q, status=status, tag=tag, workspace_id=ws
                ).subquery()
            )
        )
        rows = session.scalars(
            stmt.order_by(Recording.recorded_at.desc().nulls_last(), Recording.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return RecordingList(items=[_summary(r) for r in rows], total=total or 0)

    @app.post(
        "/api/workspaces/{workspace_id}/recordings",
        response_model=RecordingSummary,
        status_code=201,
    )
    def upload_recording(
        file: UploadFile,
        ctx: WorkspaceContext = Depends(deps.workspace_ctx),
        session: Session = Depends(db),
        user: User = Depends(deps.require_active),
        _: None = Depends(deps.require_csrf),
    ) -> RecordingSummary:
        """콘솔에서 올린 오디오를 보관 폴더에 저장하고 큐에 등록한다."""
        if cfg.upload_dir is None:
            raise HTTPException(503, "업로드가 설정돼 있지 않습니다 (UPLOAD_DIR)")
        if ctx.perm < Perm.MANAGE:
            raise HTTPException(403, "이 워크스페이스에 올릴 권한이 없습니다")
        workspace = ctx.workspace
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
                usage = measure(session, workspace)
                try:
                    check_storage(usage, staged.stat().st_size)
                except QuotaExceeded as exc:
                    raise HTTPException(413, str(exc)) from None
                # 길이는 자리를 정하기 전에 본다. 여기서 거절하면 파일이 남지 않는다
                try:
                    check_minutes(usage, probe_duration(staged))
                except QuotaExceeded as exc:
                    raise HTTPException(413, str(exc)) from None
                digest = partial_hash(staged, staged.stat().st_size)
                original = find_duplicate(session, digest, workspace_id=workspace.id)
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
                dest = finalize(
                    staged,
                    workspace_upload_dir(cfg.upload_dir, workspace.public_id),
                    name,
                    taken=registered,
                )
                recording = ingest_file(
                    session,
                    dest,
                    workspace_id=workspace.id,
                    source="upload",
                    created_by_user_id=user.id,
                )
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
    def recording_detail(
        recording: Recording = Depends(viewable),
        principal: Principal = Depends(deps.principal),
        session: Session = Depends(db),
    ) -> RecordingDetail:
        perm = resolve_recording_perm(session, principal, recording)
        state = None
        if perm >= Perm.MANAGE:
            # 공유를 관리할 수 있는 사람에게만 채운다. 콘솔이 공유 버튼의 상태를
            # 그리려고 공유 목록을 따로 부르지 않게 하는 값이다
            counts = share_counts(session, recording)
            state = ShareStateOut(user_count=counts.user_count, link_count=counts.link_count)
        return RecordingDetail(
            **_summary(recording).model_dump(),
            can_edit=perm >= Perm.EDIT,
            can_manage=perm >= Perm.MANAGE,
            share_state=state,
            error=recording.error,
            stt_meta=recording.stt_meta,
            speaker_names={n.speaker_key: n.display_name for n in recording.speaker_names},
            segments=[segment_out(s) for s in recording.segments],
        )

    @app.patch("/api/recordings/{public_id}", response_model=RecordingSummary)
    def update_title(
        body: TitleIn,
        recording: Recording = Depends(editable),
        session: Session = Depends(db),
        _: None = Depends(deps.require_csrf),
    ) -> RecordingSummary:
        recording.title = body.title.strip() or None
        session.commit()
        return _summary(recording)

    @app.put("/api/recordings/{public_id}/speakers/{speaker_key}")
    def rename_speaker(
        speaker_key: str,
        body: SpeakerNameIn,
        recording: Recording = Depends(editable),
        session: Session = Depends(db),
        _: None = Depends(deps.require_csrf),
    ) -> dict[str, str]:
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
        request: Request,
        recording: Recording = Depends(viewable),
        session: Session = Depends(db),
    ) -> Response:
        workspace = session.get(Workspace, recording.workspace_id)
        try:
            path = resolve_audio_path(
                recording.path,
                source=recording.source,
                workspace_public_id=workspace.public_id,
                upload_dir=cfg.upload_dir,
                audio_dirs=cfg.audio_dirs,
            )
        except AudioUnavailable:
            # 뿌리 밖이든 파일이 없든 같은 답이다. 구분하면 어떤 경로가 있는지 알려준다
            raise HTTPException(
                404, "오디오 파일이 없습니다 (드라이브 오프라인일 수 있음)"
            ) from None
        return range_response(path, request.headers.get("range"))

    @app.delete("/api/recordings/{public_id}", status_code=204)
    def delete_recording(
        recording: Recording = Depends(manageable),
        session: Session = Depends(db),
        _: None = Depends(deps.require_csrf),
    ) -> Response:
        """녹음을 지운다. 저장 용량 한도를 걸면서 이 길이 없으면 한도가 일방통행이 된다.

        업로드본은 디스크 원본까지 지우지만 스캔본은 DB 행만 지운다. 소유자의
        원본 파일을 서비스가 지우지 않는다. 처리 이력은 남는다: 지워진다면
        올리고-전사하고-지우고를 반복해 사용량 한도를 되돌릴 수 있다.
        """
        if recording.source == "upload":
            workspace = session.get(Workspace, recording.workspace_id)
            try:
                path = resolve_audio_path(
                    recording.path,
                    source="upload",
                    workspace_public_id=workspace.public_id,
                    upload_dir=cfg.upload_dir,
                    audio_dirs=cfg.audio_dirs,
                )
            except AudioUnavailable:
                # 이미 없거나 뿌리 밖이면 행만 지운다. 뿌리 밖 경로를 여기서 지우면
                # 봉쇄 검사를 삭제로 우회하는 길이 열린다
                path = None
            if path is not None:
                path.unlink(missing_ok=True)
        # 이 녹음을 원본으로 삼아 중복 처리된 행들을 되살린다. 안 되살리면 원본 없는
        # duplicate로 남아 워커가 집지 않고 재스캔도 못 살린다 — 파일은 폴더에 있는데
        # 아카이브에는 내용이 없는 상태가 된다
        session.execute(
            update(Recording)
            .where(
                Recording.duplicate_of_id == recording.id,
                Recording.status == "duplicate",
            )
            .values(status="pending", duplicate_of_id=None)
        )
        session.delete(recording)
        session.commit()
        return Response(status_code=204)

    @app.post("/api/recordings/{public_id}/tags", response_model=list[TagOut])
    def add_tag(
        body: TagIn,
        recording: Recording = Depends(editable),
        session: Session = Depends(db),
        _: None = Depends(deps.require_csrf),
    ) -> list[TagOut]:
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "태그 이름이 비어 있습니다")
        tag = resolve_tag(session, recording.workspace_id, name)
        if tag not in recording.tags:
            recording.tags.append(tag)
        session.commit()
        return [TagOut(id=t.public_id, name=t.name) for t in recording.tags]

    @app.delete("/api/recordings/{public_id}/tags/{tag_public_id}")
    def remove_tag(
        tag_public_id: uuid.UUID,
        recording: Recording = Depends(editable),
        session: Session = Depends(db),
        _: None = Depends(deps.require_csrf),
    ) -> list[TagOut]:
        recording.tags = [t for t in recording.tags if t.public_id != tag_public_id]
        session.commit()
        return [TagOut(id=t.public_id, name=t.name) for t in recording.tags]

    @app.get("/api/workspaces/{workspace_id}/tags", response_model=list[TagOut])
    def list_tags(
        ctx: WorkspaceContext = Depends(deps.workspace_ctx),
        session: Session = Depends(db),
    ) -> list[TagOut]:
        return [
            TagOut(id=t.public_id, name=t.name)
            for t in session.scalars(
                select(Tag).where(Tag.workspace_id == ctx.workspace.id).order_by(Tag.name)
            )
        ]

    @app.get("/api/workspaces/{workspace_id}/search", response_model=SearchResult)
    def search(
        q: str = Query(min_length=1),
        limit: int = Query(50, le=200),
        ctx: WorkspaceContext = Depends(deps.workspace_ctx),
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
                Recording.workspace_id == ctx.workspace.id,
                or_(
                    Recording.filename.ilike(pattern),
                    Recording.title.ilike(pattern),
                    Recording.summary.ilike(pattern),
                ),
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
            .where(Recording.workspace_id == ctx.workspace.id, Segment.text.ilike(pattern))
            .order_by(Recording.recorded_at.desc().nulls_last(), Recording.id.desc(), Segment.idx)
            .limit(limit)
        ).all()
        for seg, rec in seg_rows:
            hits.append(
                SearchHit(
                    recording=_summary(rec),
                    segment=segment_out(seg),
                )
            )
        return SearchResult(hits=hits[:limit])

    @app.get("/api/workspaces/{workspace_id}/usage", response_model=UsageOut)
    def workspace_usage(
        ctx: WorkspaceContext = Depends(deps.workspace_ctx),
        session: Session = Depends(db),
    ) -> UsageOut:
        usage: Usage = measure(session, ctx.workspace)
        return UsageOut(
            used_minutes=round(usage.used_minutes, 1),
            quota_minutes=usage.quota_minutes,
            used_bytes=usage.used_bytes,
            quota_bytes=usage.quota_bytes,
            window_days=usage.window_days,
        )

    @app.get("/api/workspaces/{workspace_id}/stats", response_model=Stats)
    def stats(
        ctx: WorkspaceContext = Depends(deps.workspace_ctx),
        session: Session = Depends(db),
    ) -> Stats:
        if ctx.perm < Perm.ADMIN:
            # 처리 파이프라인 뷰다. 최근 에러가 녹음 제목을 나열하므로 구성원 전체에게
            # 열면 받지 않은 녹음의 제목이 보인다
            raise HTTPException(403, "이 화면은 워크스페이스 관리자만 볼 수 있습니다")
        ws = ctx.workspace.id
        rows = session.execute(
            select(
                Recording.status,
                func.count(),
                func.coalesce(func.sum(Recording.duration_sec), 0.0),
            )
            .where(Recording.workspace_id == ws)
            .group_by(Recording.status)
        ).all()
        by_status = [StatusCount(status=s, count=c, audio_sec=float(a)) for s, c, a in rows]
        # 중복과 한도 보류는 처리 대상이 아니다. 분모에 넣으면 완료 비율이
        # 영원히 100%에 못 닿는다
        total_audio = sum(
            x.audio_sec for x in by_status if x.status not in ("duplicate", "quota_blocked")
        )
        done_audio = sum(x.audio_sec for x in by_status if x.status == "done")

        # 최근 전사 실측 20건으로 처리 배속 추정.
        # 배속은 워크스페이스로 나누지 않는다. 이건 이 기계의 특성이지 누구의
        # 데이터가 아니고, 나누면 방금 만든 워크스페이스는 표본이 없어 남은 시간을
        # 아예 못 낸다. 대신 무엇이 대기 중인지는 아래에서 자기 것만 센다
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
            # 자기 워크스페이스의 대기분만 센다. 전역으로 세면 남의 작업량이 그대로
            # 드러난다. 앞선 다른 워크스페이스의 대기열이 빠지므로 이 값은 하한이다
            pending_audio = sum(
                x.audio_sec for x in by_status if x.status in ("pending", "transcribing")
            )
            eta_sec = pending_audio * speed_ratio

        recent_errors = session.scalars(
            select(Recording)
            .options(selectinload(Recording.tags))
            .where(Recording.workspace_id == ws, Recording.status == "error")
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
    stmt: Select,
    *,
    q: str | None,
    status: str | None,
    tag: uuid.UUID | None,
    workspace_id: int,
) -> Select:
    from soriham_api.models import RecordingTag, Segment

    stmt = stmt.where(Recording.workspace_id == workspace_id)
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
