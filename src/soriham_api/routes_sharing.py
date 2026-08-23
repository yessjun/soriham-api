"""공유: 사람에게 열기, 링크로 열기, 나에게 열린 것 보기.

링크 열람자는 쿠키도 헤더도 갖지 않는다 — 토큰이 경로에 실리는 것이 그 이유다.
그래야 `<audio src>`가 그대로 동작한다.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased, selectinload

from . import auth
from .api_schemas import (
    IssuedShareLinkOut,
    SharedRecordingOut,
    SharedWithMe,
    SharedWithMeList,
    ShareIn,
    ShareLinkIn,
    ShareLinkOut,
    ShareOut,
    SharePanelOut,
    UnlockIn,
)
from .deps import Deps
from .media import range_response
from .models import Recording, RecordingShare, ShareLink, User, Workspace
from .permissions import (
    Perm,
    Principal,
    can_play_audio,
    can_see_speaker_names,
    resolve_own_perm,
)
from .ratelimit import PER_SOURCE, PER_TARGET, source_key, target_key
from .serializers import eta_sec, recording_summary, segment_out, tag_out
from .sharing import (
    LinkInvalid,
    ShareInvalid,
    create_link,
    is_unlocked,
    list_links,
    list_shares,
    load_link,
    record_view,
    revoke_link,
    revoke_share,
    share_with_email,
    unlock_value,
)
from .storage import AudioUnavailable, resolve_audio_path

# 링크마다 자기 경로로 범위를 좁힌 쿠키를 받는다. 이름이 같아도 경로가 달라 다른
# 링크에는 실리지 않는다
LINK_COOKIE = "soriham_link"
# 색인·캐시·참조 주소를 막는다
PUBLIC_HEADERS = {
    "cache-control": "private, no-store",
    "x-robots-tag": "noindex, nofollow, noarchive",
    # 방어를 한 겹 더 두는 것뿐이다. 이 정책이 실제로 물리는 자리는 토큰을 주소에
    # 담고 있는 콘솔의 공개 열람 화면이지 API 응답이 아니다
    "referrer-policy": "no-referrer",
}
_LINK_GONE = "링크가 유효하지 않습니다"


def register(app: FastAPI, deps: Deps) -> None:
    cfg = deps.cfg
    db = deps.db
    manageable = deps.recording_at(Perm.MANAGE)

    def _share_out(share: RecordingShare, user: User | None) -> ShareOut:
        return ShareOut(
            id=share.public_id,
            email=user.email if user is not None else share.invite_email or "",
            name=user.display_name if user is not None else None,
            permission=share.permission,
            pending=user is None,
        )

    def _link_out(link: ShareLink) -> ShareLinkOut:
        return ShareLinkOut(
            id=link.public_id,
            label=link.label,
            has_password=link.password_hash is not None,
            allow_audio=link.allow_audio,
            allow_speaker_names=link.allow_speaker_names,
            expires_at=link.expires_at,
            view_count=link.view_count,
            last_viewed_at=link.last_viewed_at,
            created_at=link.created_at,
        )

    @app.get("/api/recordings/{public_id}/shares", response_model=SharePanelOut)
    def share_panel(
        recording: Recording = Depends(manageable),
        session: Session = Depends(db),
    ) -> SharePanelOut:
        workspace = session.get(Workspace, recording.workspace_id)
        return SharePanelOut(
            users=[_share_out(s, u) for s, u in list_shares(session, recording)],
            links=[_link_out(x) for x in list_links(session, recording)],
            workspace_name=workspace.name if workspace is not None else "",
        )

    @app.post("/api/recordings/{public_id}/shares", response_model=ShareOut, status_code=201)
    def add_share(
        body: ShareIn,
        recording: Recording = Depends(manageable),
        session: Session = Depends(db),
        user: User = Depends(deps.require_active),
        _: None = Depends(deps.require_csrf),
    ) -> ShareOut:
        try:
            share = share_with_email(
                session,
                recording,
                email=body.email,
                permission=body.permission,
                created_by=user,
            )
        except ShareInvalid as exc:
            raise HTTPException(422, str(exc)) from None
        session.commit()
        target = session.get(User, share.user_id) if share.user_id is not None else None
        return _share_out(share, target)

    @app.delete("/api/recordings/{public_id}/shares/{share_id}", status_code=204)
    def drop_share(
        share_id: uuid.UUID,
        recording: Recording = Depends(manageable),
        session: Session = Depends(db),
        _: None = Depends(deps.require_csrf),
    ) -> Response:
        if not revoke_share(session, recording, share_id):
            raise HTTPException(404, "공유가 없습니다")
        session.commit()
        return Response(status_code=204)

    @app.post(
        "/api/recordings/{public_id}/links",
        response_model=IssuedShareLinkOut,
        status_code=201,
    )
    def add_link(
        body: ShareLinkIn,
        recording: Recording = Depends(manageable),
        session: Session = Depends(db),
        user: User = Depends(deps.require_active),
        _: None = Depends(deps.require_csrf),
    ) -> IssuedShareLinkOut:
        password = body.password or ""
        if password and not password.strip():
            # 걸었다고 믿는데 안 걸린 링크가 나가는 것이 가장 나쁘다
            raise HTTPException(422, "비밀번호가 비어 있습니다")
        try:
            issued = create_link(
                session,
                recording,
                label=body.label,
                # 다듬지 않고 그대로 해싱한다. 발급만 다듬으면 알려준 그대로 넣은
                # 열람자가 막힌다
                password_hash=auth.hash_password(password) if password else None,
                allow_audio=body.allow_audio,
                allow_speaker_names=body.allow_speaker_names,
                expires_in_days=body.expires_in_days,
                created_by=user,
            )
        except ShareInvalid as exc:
            raise HTTPException(422, str(exc)) from None
        session.commit()
        return IssuedShareLinkOut(**_link_out(issued.link).model_dump(), token=issued.token)

    @app.delete("/api/recordings/{public_id}/links/{link_id}", status_code=204)
    def drop_link(
        link_id: uuid.UUID,
        recording: Recording = Depends(manageable),
        session: Session = Depends(db),
        _: None = Depends(deps.require_csrf),
    ) -> Response:
        if not revoke_link(session, recording, link_id):
            raise HTTPException(404, "링크가 없습니다")
        session.commit()
        return Response(status_code=204)

    @app.get("/api/shared-with-me", response_model=SharedWithMeList)
    def shared_with_me(
        user: User = Depends(deps.require_active),
        session: Session = Depends(db),
        limit: int = Query(50, le=200),
        offset: int = 0,
    ) -> SharedWithMeList:
        """워크스페이스 밖에서 나에게 열린 녹음들.

        목록과 검색은 워크스페이스 필터만 걸어서 여기 안 나오면 공유받은 사람이 그
        녹음에 닿을 길이 없다. 권한과 공유한 사람을 함께 준다 — 없으면 항목마다 상세를
        다시 불러야 알 수 있다.
        """
        sharer = aliased(User)
        total = session.scalar(
            select(func.count(RecordingShare.id)).where(RecordingShare.user_id == user.id)
        )
        rows = session.execute(
            select(Recording, RecordingShare.permission, sharer.display_name)
            .join(RecordingShare, RecordingShare.recording_id == Recording.id)
            .outerjoin(sharer, sharer.id == RecordingShare.created_by_user_id)
            .options(selectinload(Recording.tags))
            .where(RecordingShare.user_id == user.id)
            .order_by(Recording.recorded_at.desc().nulls_last(), Recording.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return SharedWithMeList(
            items=[
                SharedWithMe(
                    **recording_summary(rec).model_dump(),
                    permission=permission,
                    shared_by=shared_by,
                )
                for rec, permission, shared_by in rows
            ],
            total=total or 0,
        )

    # --- 링크로 여는 공개 표면 -------------------------------------------------

    def _open(session: Session, token: str) -> tuple[ShareLink, Recording]:
        try:
            link = load_link(session, token)
        except LinkInvalid:
            raise HTTPException(404, _LINK_GONE) from None
        recording = session.scalar(
            select(Recording)
            .where(Recording.id == link.recording_id)
            .options(
                selectinload(Recording.tags),
                selectinload(Recording.segments),
                selectinload(Recording.speaker_names),
            )
        )
        if recording is None:
            raise HTTPException(404, _LINK_GONE)
        return link, recording

    def _viewer(
        session: Session, link: ShareLink, recording: Recording, principal: Principal
    ) -> tuple[Principal, Perm]:
        """링크 열람자로서의 주체와, 링크를 뺀 자기 권한.

        로그인한 채로 링크를 열면 둘 다 성립한다. 잠금은 자기 권한이 없는 사람에게만
        의미가 있다 — 구성원은 어차피 다른 길로 같은 것을 본다.
        """
        as_viewer = replace(
            principal,
            link_recording_id=recording.id,
            link_allows_audio=link.allow_audio,
            link_allows_speaker_names=link.allow_speaker_names,
        )
        # 링크를 얹은 쪽에서 다시 빼서 묻는다. 링크가 없는 주체를 넘겨 우연히 맞히면,
        # 나중에 누가 순서를 바꾸는 순간 잠금이 조용히 풀린다
        return as_viewer, resolve_own_perm(session, as_viewer, recording)

    def _require_unlocked(request: Request, link: ShareLink, own: Perm) -> None:
        if own >= Perm.VIEW:
            return
        if is_unlocked(link, request.cookies.get(LINK_COOKIE)):
            return
        # 오디오만 막으면 요약이 비밀번호 없이 샌다. 상세도 같은 관문을 지난다
        raise HTTPException(401, "비밀번호가 필요합니다")

    @app.post("/api/shared/{token}/unlock", status_code=204)
    def unlock(
        token: str,
        body: UnlockIn,
        request: Request,
        response: Response,
        session: Session = Depends(db),
    ) -> Response:
        """비밀번호를 확인하고 이 링크 경로로 범위를 좁힌 쿠키를 준다.

        CSRF 헤더를 요구하지 않는다 — 세션이 없는 사람이 쓰는 길이고, 남의 브라우저가
        이 요청을 대신 보내려면 토큰과 비밀번호를 이미 알아야 한다.
        """
        ip = request.client.host if request.client else None
        # 인터넷에 열린 argon2 호출이 로그인 말고 여기 하나 더 있다. 토큰을 가진
        # 사람이 비밀번호를 무한정 두드리는 것을 막는다
        deps.guard_attempts(
            [(source_key("unlock", ip), PER_SOURCE), (target_key("unlock", ip, token), PER_TARGET)]
        )
        link, _recording = _open(session, token)
        if link.password_hash is None:
            # 잠기지 않은 링크에 대고 부른 것이다. 쿠키를 주지 않고 조용히 끝낸다
            return Response(status_code=204, headers=PUBLIC_HEADERS)
        if not auth.verify_password(link.password_hash, body.password):
            raise HTTPException(403, "비밀번호가 올바르지 않습니다")
        deps.clear_attempts([source_key("unlock", ip), target_key("unlock", ip, token)])
        out = Response(status_code=204, headers=PUBLIC_HEADERS)
        out.set_cookie(
            LINK_COOKIE,
            unlock_value(link),
            httponly=True,
            secure=cfg.cookie_secure,
            samesite="lax",
            # 브라우저를 닫으면 사라진다. 남의 자리에서 연 링크가 남지 않게
            path=f"/api/shared/{token}",
        )
        return out

    @app.get("/api/shared/{token}", response_model=SharedRecordingOut)
    def shared_detail(
        token: str,
        request: Request,
        response: Response,
        session: Session = Depends(db),
        principal: Principal = Depends(deps.principal),
    ) -> SharedRecordingOut:
        link, recording = _open(session, token)
        as_viewer, own = _viewer(session, link, recording, principal)
        _require_unlocked(request, link, own)
        response.headers.update(PUBLIC_HEADERS)
        record_view(session, link)
        session.commit()
        names = (
            {n.speaker_key: n.display_name for n in recording.speaker_names}
            if can_see_speaker_names(as_viewer, own)
            else {}
        )
        return SharedRecordingOut(
            title=recording.title,
            summary=recording.summary,
            recorded_at=recording.recorded_at,
            duration_sec=recording.duration_sec,
            status=recording.status,
            language=recording.language,
            tags=[tag_out(t) for t in recording.tags],
            progress=recording.progress,
            eta_sec=eta_sec(recording),
            allow_audio=can_play_audio(as_viewer, own),
            speaker_names=names,
            segments=[segment_out(s) for s in recording.segments],
        )

    @app.get("/api/shared/{token}/audio")
    def shared_audio(
        token: str,
        request: Request,
        session: Session = Depends(db),
        principal: Principal = Depends(deps.principal),
    ) -> Response:
        link, recording = _open(session, token)
        as_viewer, own = _viewer(session, link, recording, principal)
        _require_unlocked(request, link, own)
        if not can_play_audio(as_viewer, own):
            # 링크 주인은 이 녹음이 있다는 것을 이미 안다. 감출 것이 없다
            raise HTTPException(403, "이 링크는 오디오를 재생할 수 없습니다")
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
            raise HTTPException(
                404, "오디오 파일이 없습니다 (드라이브 오프라인일 수 있음)"
            ) from None
        return range_response(path, request.headers.get("range"), headers=PUBLIC_HEADERS)
