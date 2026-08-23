"""사용자와 워크스페이스를 만들고 찾는다.

승인 흐름과 초대는 라우트가 붙을 때 여기 이어진다. 지금은 계정과 워크스페이스를
만들고 찾는 것까지다.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Tag, User, Workspace, WorkspaceMember

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class WorkspaceNotFound(Exception):
    """설정이 가리키는 워크스페이스가 없다."""


def normalize_email(raw: str) -> str:
    """비교와 저장에 쓰는 단일 형태. 스키마의 lower() CHECK가 이 함수를 강제한다."""
    return raw.strip().lower()


def validate_slug(slug: str) -> str:
    slug = slug.strip().lower()
    if not _SLUG_RE.match(slug):
        raise ValueError(f"워크스페이스 슬러그는 소문자·숫자·하이픈만 됩니다: {slug!r}")
    return slug


def get_workspace(session: Session, slug: str) -> Workspace:
    workspace = session.scalar(select(Workspace).where(Workspace.slug == slug))
    if workspace is None:
        raise WorkspaceNotFound(f"워크스페이스가 없습니다: {slug}")
    return workspace


def find_workspace(session: Session, slug: str) -> Workspace | None:
    return session.scalar(select(Workspace).where(Workspace.slug == slug))


def find_user(session: Session, email: str) -> User | None:
    return session.scalar(select(User).where(User.email == normalize_email(email)))


def create_workspace(
    session: Session,
    *,
    slug: str,
    name: str,
    kind: str = "team",
    quota_minutes: int | None = None,
    quota_bytes: int | None = None,
) -> Workspace:
    workspace = Workspace(
        slug=validate_slug(slug),
        name=name,
        kind=kind,
        quota_minutes=quota_minutes,
        quota_bytes=quota_bytes,
    )
    session.add(workspace)
    session.flush()
    return workspace


def create_user(
    session: Session,
    *,
    email: str,
    password_hash: str,
    display_name: str,
    status: str = "pending",
    is_service_admin: bool = False,
    signup_note: str | None = None,
) -> User:
    """계정을 만든다. 기본은 승인 대기다 — 가입은 열려 있고 관문은 상태다."""
    user = User(
        email=normalize_email(email),
        password_hash=password_hash,
        display_name=display_name,
        status=status,
        is_service_admin=is_service_admin,
        signup_note=signup_note,
    )
    session.add(user)
    session.flush()
    return user


def add_member(session: Session, workspace: Workspace, user: User, role: str) -> WorkspaceMember:
    member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=role)
    session.add(member)
    session.flush()
    return member


def resolve_tag(session: Session, workspace_id: int, name: str) -> Tag:
    """워크스페이스 안에서 태그를 찾거나 만든다.

    이름만으로 찾으면 다른 워크스페이스가 만든 같은 이름의 태그를 그대로 물려받는다 —
    "인사평가" 하나가 두 조직에 걸린다.
    """
    tag = session.scalar(select(Tag).where(Tag.workspace_id == workspace_id, Tag.name == name))
    if tag is None:
        tag = Tag(workspace_id=workspace_id, name=name)
        session.add(tag)
        session.flush()
    return tag
