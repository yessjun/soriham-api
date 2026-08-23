"""요청에서 주체를 읽고 권한을 확인하는 의존성.

경로 파라미터로 녹음을 얻는 길은 `recording_at(need)` 하나뿐이다. 다른 길을 두면
그 길을 쓰는 라우트가 조용히 무범위로 샌다 — 실제로 상세 라우트가 공용 조회 함수를
쓰지 않고 자기 질의를 인라인하고 있었다.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from . import auth
from .config import Settings
from .models import Recording, User, Workspace, WorkspaceMember
from .permissions import Perm, Principal, resolve_recording_perm, resolve_workspace_perm

# 안전하지 않은 메서드에만 CSRF 헤더를 요구한다. GET을 면제하는 것이 <audio> 태그가
# 도는 보증이다 — 그 태그는 헤더를 실을 수 없다
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class WorkspaceContext:
    workspace: Workspace
    perm: Perm


@dataclass
class Deps:
    """`create_app`이 만들고 라우트 등록 함수들이 나눠 쓰는 묶음."""

    cfg: Settings
    factory: sessionmaker[Session]
    db: Callable[..., Iterator[Session]]
    principal: Callable[..., Principal]
    require_user: Callable[..., User]
    require_active: Callable[..., User]
    require_csrf: Callable[..., None]
    workspace_ctx: Callable[..., WorkspaceContext]
    recording_at: Callable[[Perm], Callable[..., Recording]]
    require_service_admin: Callable[..., User]
    upload_lock: object


def build_deps(cfg: Settings, factory: sessionmaker[Session], upload_lock: object) -> Deps:
    def db() -> Iterator[Session]:
        with factory() as session:
            yield session

    def current_principal(request: Request, session: Session = Depends(db)) -> Principal:
        """쿠키에서 주체를 만든다. 없으면 익명이다.

        링크 열람자는 쿠키가 아니라 경로에 실린 토큰으로 인가받으므로 여기 오지 않는다.
        """
        raw = request.cookies.get(cfg.cookie_name)
        if not raw:
            return Principal.anonymous()
        stored = auth.load_session(session, raw)
        if stored is None:
            return Principal.anonymous()
        user = session.get(User, stored.user_id)
        if user is None:
            return Principal.anonymous()
        request.state.session_row = stored
        request.state.user = user
        auth.touch_session(session, stored)
        session.commit()
        return Principal.for_user(user)

    def require_user(request: Request, principal: Principal = Depends(current_principal)) -> User:
        """로그인만 확인한다. 승인 여부는 보지 않는다.

        인증과 인가를 섞지 않는 자리다. 대기 중인 사람도 로그인에 성공하고 자기 상태를
        조회할 수 있어야 콘솔이 "왜 못 쓰는지"를 화면에 그린다.
        """
        user = getattr(request.state, "user", None)
        if user is None or not principal.is_authenticated:
            raise HTTPException(401, "로그인이 필요합니다")
        return user

    def require_active(user: User = Depends(require_user)) -> User:
        if user.status == "pending":
            raise HTTPException(403, "관리자 승인 대기 중입니다")
        if user.status != "active":
            raise HTTPException(403, "사용할 수 없는 계정입니다")
        return user

    def require_service_admin(user: User = Depends(require_active)) -> User:
        if not user.is_service_admin:
            raise HTTPException(404, "찾을 수 없습니다")
        return user

    def require_csrf(request: Request, user: User = Depends(require_user)) -> None:
        """쿠키가 자동으로 실리는 만큼 교차 사이트 요청을 한 겹 더 막는다."""
        if request.method in SAFE_METHODS:
            return
        stored = getattr(request.state, "session_row", None)
        sent = request.headers.get(CSRF_HEADER)
        if stored is None or not sent or sent != stored.csrf_token:
            raise HTTPException(403, "요청이 만료됐습니다. 새로고침 후 다시 시도하세요")

    def workspace_ctx(
        workspace_id: uuid.UUID,
        user: User = Depends(require_active),
        principal: Principal = Depends(current_principal),
        session: Session = Depends(db),
    ) -> WorkspaceContext:
        workspace = session.scalar(select(Workspace).where(Workspace.public_id == workspace_id))
        if workspace is None:
            raise HTTPException(404, "워크스페이스가 없습니다")
        perm = resolve_workspace_perm(session, principal, workspace.id)
        if perm < Perm.VIEW:
            # 없는 것과 권한 없는 것을 구분해 주지 않는다
            raise HTTPException(404, "워크스페이스가 없습니다")
        return WorkspaceContext(workspace=workspace, perm=perm)

    def recording_at(need: Perm) -> Callable[..., Recording]:
        """public_id로 녹음을 얻는 유일한 통로. 권한이 모자라면 없는 것처럼 답한다."""

        def dep(
            public_id: uuid.UUID,
            principal: Principal = Depends(current_principal),
            session: Session = Depends(db),
        ) -> Recording:
            if not principal.is_authenticated:
                # 존재 여부를 감추기 전에 "로그인하라"를 먼저 말한다. 어떤 id를 넣어도
                # 같은 답이라 탐침이 되지 않고, 세션이 만료된 사람이 로그인 화면으로
                # 돌아갈 수 있다 — 404를 주면 콘솔은 없는 녹음이라고 표시한다
                raise HTTPException(401, "로그인이 필요합니다")
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
            if _blocked_account(session, principal):
                raise HTTPException(403, "관리자 승인 대기 중입니다")
            if resolve_recording_perm(session, principal, recording) < need:
                # 403이면 그 녹음이 있다고 알려주는 셈이라 public_id 공간이
                # 멤버십 탐침이 된다
                raise HTTPException(404, "녹음이 없습니다")
            return recording

        return dep

    def _blocked_account(session: Session, principal: Principal) -> bool:
        if principal.user_id is None:
            return False
        status = session.scalar(select(User.status).where(User.id == principal.user_id))
        return status != "active"

    return Deps(
        cfg=cfg,
        factory=factory,
        db=db,
        principal=current_principal,
        require_user=require_user,
        require_active=require_active,
        require_csrf=require_csrf,
        workspace_ctx=workspace_ctx,
        recording_at=recording_at,
        require_service_admin=require_service_admin,
        upload_lock=upload_lock,
    )


def user_workspaces(session: Session, user: User) -> list[Workspace]:
    return list(
        session.scalars(
            select(Workspace)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(WorkspaceMember.user_id == user.id)
            .order_by(Workspace.name)
        ).all()
    )
