"""가입·로그인·내 정보, 그리고 승인 관리."""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import auth
from .api_schemas import (
    LoginIn,
    MeOut,
    PasswordChangeIn,
    PendingUserOut,
    SignupIn,
    UserOut,
    WorkspaceRef,
)
from .deps import Deps, user_workspaces
from .models import User, Workspace, WorkspaceMember
from .tenancy import EmailTaken, approve, find_user, reject, signup

# 로그인 실패 문구는 어느 쪽이 틀렸는지 알려주지 않는다
_BAD_LOGIN = "이메일 또는 비밀번호가 올바르지 않습니다"


def _user_out(user: User) -> UserOut:
    return UserOut(id=user.public_id, email=user.email, name=user.display_name)


def _capabilities(session: Session, user: User) -> list[str]:
    """콘솔이 무엇을 그릴지 정하는 값. 역할 산술을 클라이언트에서 다시 하지 않게 한다."""
    if user.status != "active":
        return []
    caps = ["upload"]
    if user.is_service_admin:
        caps += ["admin", "stats", "create_workspace"]
        return caps
    elevated = session.scalar(
        select(func.count(WorkspaceMember.id)).where(
            WorkspaceMember.user_id == user.id,
            WorkspaceMember.role.in_(("owner", "admin")),
        )
    )
    if elevated:
        caps += ["invite", "stats"]
    return caps


def _me(session: Session, user: User) -> MeOut:
    workspaces = user_workspaces(session, user)
    roles = dict(
        session.execute(
            select(WorkspaceMember.workspace_id, WorkspaceMember.role).where(
                WorkspaceMember.user_id == user.id
            )
        ).all()
    )
    default = (
        session.get(Workspace, user.default_workspace_id)
        if user.default_workspace_id is not None
        else None
    )
    pending = None
    if user.is_service_admin and user.status == "active":
        pending = session.scalar(select(func.count(User.id)).where(User.status == "pending"))
    return MeOut(
        user=_user_out(user),
        status=user.status,
        workspaces=[
            WorkspaceRef(id=w.public_id, name=w.name, slug=w.slug, role=roles.get(w.id, "member"))
            for w in workspaces
        ],
        default_workspace_id=default.public_id if default is not None else None,
        capabilities=_capabilities(session, user),
        pending_user_count=pending,
    )


def register(app: FastAPI, deps: Deps) -> None:
    cfg = deps.cfg

    def _set_cookie(response: Response, token: str, csrf: str) -> None:
        max_age = auth.SESSION_IDLE_DAYS * 24 * 3600
        response.set_cookie(
            cfg.cookie_name,
            token,
            httponly=True,
            secure=cfg.cookie_secure,
            samesite="lax",
            path="/",
            domain=cfg.cookie_domain,
            max_age=max_age,
        )
        # 자바스크립트가 읽어 헤더로 돌려보내는 값. 쿠키가 자동으로 실리는 만큼
        # "이 요청을 우리 화면이 보냈다"는 증거가 따로 필요하다
        response.set_cookie(
            "soriham_csrf",
            csrf,
            httponly=False,
            secure=cfg.cookie_secure,
            samesite="lax",
            path="/",
            domain=cfg.cookie_domain,
            max_age=max_age,
        )

    def _clear_cookie(response: Response) -> None:
        for name in (cfg.cookie_name, "soriham_csrf"):
            response.delete_cookie(name, path="/", domain=cfg.cookie_domain)

    @app.post("/api/auth/signup", response_model=MeOut, status_code=201)
    def signup_route(
        body: SignupIn,
        request: Request,
        response: Response,
        session: Session = Depends(deps.db),
    ) -> MeOut:
        if not body.password.strip():
            raise HTTPException(422, "비밀번호가 비어 있습니다")
        if not body.display_name.strip():
            raise HTTPException(422, "이름이 비어 있습니다")
        try:
            result = signup(
                session,
                email=body.email,
                password=body.password,
                display_name=body.display_name.strip(),
                signup_note=body.signup_note,
                auto_approve=cfg.auto_approve,
            )
        except EmailTaken:
            raise HTTPException(409, "이미 가입된 이메일입니다") from None
        except IntegrityError:
            # 같은 이메일이 동시에 들어온 경우 — 앞의 검사와 삽입 사이의 틈
            session.rollback()
            raise HTTPException(409, "이미 가입된 이메일입니다") from None

        issued = auth.create_session(
            session,
            result.user,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
        session.commit()
        _set_cookie(response, issued.token, issued.csrf_token)
        return _me(session, result.user)

    @app.post("/api/auth/login", response_model=MeOut)
    def login_route(
        body: LoginIn,
        request: Request,
        response: Response,
        session: Session = Depends(deps.db),
    ) -> MeOut:
        user = find_user(session, body.email)
        stored = user.password_hash if user is not None else None
        # 계정이 없어도 같은 검증을 돌린다 — 응답 시간이 존재 여부를 알려주지 않게
        if not auth.verify_password(stored, body.password):
            raise HTTPException(401, _BAD_LOGIN)
        assert user is not None
        if user.status == "rejected":
            raise HTTPException(403, "가입이 거절된 계정입니다")
        if user.status == "disabled":
            raise HTTPException(403, "사용이 중지된 계정입니다")

        if auth.needs_rehash(user.password_hash):
            user.password_hash = auth.hash_password(body.password)
        user.last_login_at = func.now()
        issued = auth.create_session(
            session,
            user,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
        session.commit()
        _set_cookie(response, issued.token, issued.csrf_token)
        return _me(session, user)

    @app.post("/api/auth/logout", status_code=204)
    def logout_route(
        request: Request, response: Response, session: Session = Depends(deps.db)
    ) -> Response:
        raw = request.cookies.get(cfg.cookie_name)
        if raw:
            stored = auth.load_session(session, raw)
            if stored is not None:
                auth.revoke_session(session, stored)
                session.commit()
        _clear_cookie(response)
        return Response(status_code=204)

    @app.get("/api/auth/me", response_model=MeOut)
    def me_route(
        user: User = Depends(deps.require_user), session: Session = Depends(deps.db)
    ) -> MeOut:
        """승인 대기 중에도 응답한다 — 콘솔이 왜 못 쓰는지를 그려야 한다."""
        return _me(session, user)

    @app.post("/api/auth/password", status_code=204)
    def change_password(
        body: PasswordChangeIn,
        request: Request,
        response: Response,
        user: User = Depends(deps.require_user),
        session: Session = Depends(deps.db),
        _: None = Depends(deps.require_csrf),
    ) -> Response:
        if not auth.verify_password(user.password_hash, body.current_password):
            raise HTTPException(403, "현재 비밀번호가 올바르지 않습니다")
        if not body.new_password.strip():
            raise HTTPException(422, "새 비밀번호가 비어 있습니다")
        user.password_hash = auth.hash_password(body.new_password)
        # 비밀번호가 바뀌면 다른 자리의 세션은 죽어야 한다. 지금 이 자리는 살린다
        current = getattr(request.state, "session_row", None)
        auth.revoke_other_sessions(session, user, keep=current)
        session.commit()
        return Response(status_code=204)

    @app.get("/api/admin/pending", response_model=list[PendingUserOut])
    def list_pending(
        admin: User = Depends(deps.require_service_admin),
        session: Session = Depends(deps.db),
    ) -> list[PendingUserOut]:
        rows = session.scalars(
            select(User).where(User.status == "pending").order_by(User.created_at)
        ).all()
        return [
            PendingUserOut(
                id=row.public_id,
                email=row.email,
                name=row.display_name,
                signup_note=row.signup_note,
                requested_at=row.created_at,
            )
            for row in rows
        ]

    @app.post("/api/admin/pending/{user_public_id}/approve", response_model=MeOut)
    def approve_route(
        user_public_id: uuid.UUID,
        admin: User = Depends(deps.require_service_admin),
        session: Session = Depends(deps.db),
        _: None = Depends(deps.require_csrf),
    ) -> MeOut:
        target = _target_user(session, user_public_id)
        approve(session, target, reviewer=admin)
        session.commit()
        return _me(session, target)

    @app.post("/api/admin/pending/{user_public_id}/reject", status_code=204)
    def reject_route(
        user_public_id: uuid.UUID,
        admin: User = Depends(deps.require_service_admin),
        session: Session = Depends(deps.db),
        _: None = Depends(deps.require_csrf),
    ) -> Response:
        target = _target_user(session, user_public_id)
        if target.id == admin.id:
            raise HTTPException(422, "자기 계정은 거절할 수 없습니다")
        reject(session, target, reviewer=admin)
        session.commit()
        return Response(status_code=204)

    def _target_user(session: Session, public_id: uuid.UUID) -> User:
        target = session.scalar(select(User).where(User.public_id == public_id))
        if target is None:
            raise HTTPException(404, "계정이 없습니다")
        return target
