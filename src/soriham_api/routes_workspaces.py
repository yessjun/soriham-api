"""워크스페이스 관리: 구성원과 초대, 그리고 워크스페이스 자체.

스키마와 서비스 계층은 앞선 작업에서 만들어졌는데 부를 길이 없었다. `/api/auth/me`가
`invite` 능력을 내주면서 정작 초대를 발급할 자리가 없는 상태였다.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .api_schemas import (
    InviteIn,
    InviteOut,
    InvitePreviewOut,
    IssuedInviteOut,
    MemberOut,
    RoleIn,
    UserOut,
    WorkspaceCreateIn,
    WorkspaceRef,
)
from .deps import Deps, WorkspaceContext
from .models import Invite, User, Workspace, WorkspaceMember
from .permissions import Perm
from .routes_auth import workspace_ref
from .tenancy import (
    InviteInvalid,
    MemberInvalid,
    accept_invite,
    add_member,
    create_invite,
    create_workspace,
    list_invites,
    list_members,
    peek_invite,
    remove_member,
    revoke_invite,
    set_member_role,
    validate_slug,
)


def register(app: FastAPI, deps: Deps) -> None:
    cfg = deps.cfg
    db = deps.db

    def _require_admin(ctx: WorkspaceContext) -> WorkspaceContext:
        if ctx.perm < Perm.ADMIN:
            raise HTTPException(403, "이 워크스페이스의 관리자만 할 수 있습니다")
        return ctx

    def _member_target(session: Session, workspace: Workspace, user_public_id: uuid.UUID) -> User:
        user = session.scalar(select(User).where(User.public_id == user_public_id))
        if user is None:
            raise HTTPException(404, "구성원이 없습니다")
        return user

    def _member_out(member: WorkspaceMember, user: User) -> MemberOut:
        return MemberOut(
            user=UserOut(id=user.public_id, email=user.email, name=user.display_name),
            role=member.role,
            status=user.status,
            joined_at=member.created_at,
        )

    def _invite_out(invite: Invite) -> InviteOut:
        return InviteOut(
            id=invite.public_id,
            email=invite.email,
            role=invite.role,
            expires_at=invite.expires_at,
            max_uses=invite.max_uses,
            uses=invite.uses,
            created_at=invite.created_at,
        )

    @app.get("/api/workspaces/{workspace_id}/members", response_model=list[MemberOut])
    def workspace_members(
        ctx: WorkspaceContext = Depends(deps.workspace_ctx),
        session: Session = Depends(db),
    ) -> list[MemberOut]:
        """구성원 목록. 같은 워크스페이스 사람끼리는 서로를 안다."""
        return [_member_out(member, user) for member, user in list_members(session, ctx.workspace)]

    @app.put("/api/workspaces/{workspace_id}/members/{user_public_id}", response_model=MemberOut)
    def change_role(
        user_public_id: uuid.UUID,
        body: RoleIn,
        ctx: WorkspaceContext = Depends(deps.workspace_ctx),
        session: Session = Depends(db),
        _: None = Depends(deps.require_csrf),
    ) -> MemberOut:
        _require_admin(ctx)
        target = _member_target(session, ctx.workspace, user_public_id)
        try:
            member = set_member_role(session, ctx.workspace, target, body.role)
        except MemberInvalid as exc:
            raise HTTPException(422, str(exc)) from None
        session.commit()
        return _member_out(member, target)

    @app.delete("/api/workspaces/{workspace_id}/members/{user_public_id}", status_code=204)
    def drop_member(
        user_public_id: uuid.UUID,
        ctx: WorkspaceContext = Depends(deps.workspace_ctx),
        session: Session = Depends(db),
        _: None = Depends(deps.require_csrf),
    ) -> Response:
        _require_admin(ctx)
        target = _member_target(session, ctx.workspace, user_public_id)
        try:
            remove_member(session, ctx.workspace, target)
        except MemberInvalid as exc:
            raise HTTPException(422, str(exc)) from None
        session.commit()
        return Response(status_code=204)

    @app.get("/api/workspaces/{workspace_id}/invites", response_model=list[InviteOut])
    def workspace_invites(
        ctx: WorkspaceContext = Depends(deps.workspace_ctx),
        session: Session = Depends(db),
    ) -> list[InviteOut]:
        _require_admin(ctx)
        return [_invite_out(x) for x in list_invites(session, ctx.workspace)]

    @app.post(
        "/api/workspaces/{workspace_id}/invites",
        response_model=IssuedInviteOut,
        status_code=201,
    )
    def issue_invite(
        body: InviteIn,
        ctx: WorkspaceContext = Depends(deps.workspace_ctx),
        session: Session = Depends(db),
        user: User = Depends(deps.require_active),
        _: None = Depends(deps.require_csrf),
    ) -> IssuedInviteOut:
        _require_admin(ctx)
        try:
            issued = create_invite(
                session,
                workspace=ctx.workspace,
                role=body.role,
                email=body.email,
                created_by=user,
                expires_in_days=body.expires_in_days,
                max_uses=body.max_uses,
            )
        except (MemberInvalid, ValueError) as exc:
            raise HTTPException(422, str(exc)) from None
        session.commit()
        return IssuedInviteOut(**_invite_out(issued.invite).model_dump(), token=issued.token)

    @app.delete("/api/workspaces/{workspace_id}/invites/{invite_id}", status_code=204)
    def drop_invite(
        invite_id: uuid.UUID,
        ctx: WorkspaceContext = Depends(deps.workspace_ctx),
        session: Session = Depends(db),
        _: None = Depends(deps.require_csrf),
    ) -> Response:
        _require_admin(ctx)
        if not revoke_invite(session, ctx.workspace, invite_id):
            raise HTTPException(404, "초대가 없습니다")
        session.commit()
        return Response(status_code=204)

    @app.get("/api/invites/{token}", response_model=InvitePreviewOut)
    def invite_preview(
        token: str,
        session: Session = Depends(db),
        user: User = Depends(deps.require_active),
    ) -> InvitePreviewOut:
        """어디로 부르는 초대인지만 알려준다. 구성원 목록도 녹음도 아직 아니다."""
        try:
            invite = peek_invite(session, token, user)
        except InviteInvalid:
            raise HTTPException(404, "초대가 유효하지 않습니다") from None
        workspace = session.get(Workspace, invite.workspace_id)
        if workspace is None:
            raise HTTPException(404, "초대가 유효하지 않습니다")
        return InvitePreviewOut(workspace_name=workspace.name, role=invite.role)

    @app.post("/api/invites/{token}/accept", response_model=WorkspaceRef)
    def take_invite(
        token: str,
        session: Session = Depends(db),
        user: User = Depends(deps.require_active),
        _: None = Depends(deps.require_csrf),
    ) -> WorkspaceRef:
        try:
            workspace = accept_invite(session, token, user)
        except InviteInvalid:
            raise HTTPException(404, "초대가 유효하지 않습니다") from None
        if user.default_workspace_id is None:
            user.default_workspace_id = workspace.id
        session.commit()
        role = session.scalar(
            select(WorkspaceMember.role).where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.user_id == user.id,
            )
        )
        return workspace_ref(workspace, role or "member")

    @app.post("/api/workspaces", response_model=WorkspaceRef, status_code=201)
    def new_workspace(
        body: WorkspaceCreateIn,
        session: Session = Depends(db),
        admin: User = Depends(deps.require_service_admin),
        _: None = Depends(deps.require_csrf),
    ) -> WorkspaceRef:
        """팀 워크스페이스는 서비스 관리자만 만든다.

        한도가 워크스페이스에 붙으므로 누구나 만들 수 있으면 한도가 무의미해진다.
        """
        try:
            slug = validate_slug(body.slug)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if session.scalar(select(Workspace.id).where(Workspace.slug == slug)) is not None:
            raise HTTPException(409, "이미 있는 슬러그입니다")
        if not body.name.strip():
            raise HTTPException(422, "이름이 비어 있습니다")
        workspace = create_workspace(
            session,
            slug=slug,
            name=body.name.strip(),
            kind="team",
            quota_minutes=cfg.default_quota_minutes,
            quota_bytes=cfg.default_quota_bytes,
        )
        add_member(session, workspace, admin, "owner")
        session.commit()
        return workspace_ref(workspace, "owner")
