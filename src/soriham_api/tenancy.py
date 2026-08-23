"""가입과 승인, 워크스페이스와 구성원.

가입은 누구나 할 수 있고 **계정 상태가 관문이다.** 신청하면 대기 상태로 계정과 개인
워크스페이스가 함께 생기고, 승인은 상태 한 번 뒤집기다. 그때 가입 전에 이메일로
걸어둔 공유와 초대가 실체가 된다.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .auth import hash_password, new_token, token_hash
from .models import (
    WORKSPACE_ROLES,
    Invite,
    Recording,
    RecordingShare,
    Tag,
    User,
    Workspace,
    WorkspaceMember,
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9-]+")

INVITE_DEFAULT_DAYS = 14
# 사람이 손으로 넣는 값이라 상한을 둔다. 무기한은 일수가 아니라 None으로 말한다
INVITE_MAX_DAYS = 3650
# 초대로는 소유자가 될 수 없다. 소유권 이전은 범위 밖이고, 허용하면 부분 유니크
# 인덱스가 IntegrityError로 거절해 사용자에게 500으로 보인다
INVITABLE_ROLES = tuple(r for r in WORKSPACE_ROLES if r != "owner")


class WorkspaceNotFound(Exception):
    """설정이 가리키는 워크스페이스가 없다."""


class EmailTaken(Exception):
    """이미 있는 이메일로 가입을 시도했다."""


class InviteInvalid(Exception):
    """초대가 없거나 만료·철회됐거나 다 쓰였다."""


class MemberInvalid(Exception):
    """구성원 변경이나 초대 발급을 할 수 없다. 메시지는 사용자에게 그대로 보여진다."""


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


def slugify(raw: str, *, fallback: str = "ws") -> str:
    """표시 이름에서 슬러그 후보를 만든다. 개인 워크스페이스 이름 짓기에 쓴다."""
    candidate = _SLUG_STRIP_RE.sub("-", raw.strip().lower()).strip("-")
    return candidate or fallback


def unique_slug(session: Session, base: str) -> str:
    """비어 있는 슬러그를 찾는다. 이메일 앞부분이 겹치는 일은 흔하다."""
    base = slugify(base)
    if find_workspace(session, base) is None:
        return base
    while True:
        candidate = f"{base}-{secrets.token_hex(3)}"
        if find_workspace(session, candidate) is None:
            return candidate


@dataclass(frozen=True)
class SignupResult:
    user: User
    workspace: Workspace


def signup(
    session: Session,
    *,
    email: str,
    password: str,
    display_name: str,
    signup_note: str | None = None,
    auto_approve: bool = False,
    quota_minutes: int | None = None,
    quota_bytes: int | None = None,
) -> SignupResult:
    """가입 신청. 대기 상태로 계정과 개인 워크스페이스를 함께 만든다.

    워크스페이스를 승인 때가 아니라 여기서 만드는 이유: 승인이 상태 한 번 뒤집기로
    끝나면 승인 경로에 실패할 자리가 없다. 거절하면 함께 지운다.

    한도는 호출자가 설정에서 읽어 넘긴다. 여기서 기본값을 비워 두면 승인이 곧
    무제한이 되어 한도 장치 전체가 아무도 막지 않는다.
    """
    normalized = normalize_email(email)
    if find_user(session, normalized) is not None:
        raise EmailTaken(f"이미 가입된 이메일입니다: {normalized}")

    user = create_user(
        session,
        email=normalized,
        password_hash=hash_password(password),
        display_name=display_name,
        status="active" if auto_approve else "pending",
        signup_note=signup_note,
    )
    workspace = create_workspace(
        session,
        slug=unique_slug(session, normalized.split("@")[0]),
        name=f"{display_name}의 보관함",
        kind="personal",
        quota_minutes=quota_minutes,
        quota_bytes=quota_bytes,
    )
    add_member(session, workspace, user, "owner")
    user.default_workspace_id = workspace.id
    session.flush()
    if auto_approve:
        claim_pending(session, user)
    return SignupResult(user=user, workspace=workspace)


def approve(
    session: Session,
    user: User,
    *,
    reviewer: User | None = None,
    quota_minutes: int | None = None,
    quota_bytes: int | None = None,
) -> User:
    """승인. 가입 전에 이메일로 걸어둔 공유와 초대가 이때 실체가 된다.

    거절이 개인 워크스페이스를 지우므로, 되돌리는 승인은 그것도 되돌려야 한다.
    이메일이 남아 재가입이 막히기 때문에 승인이 유일한 복구 경로다.
    """
    user.status = "active"
    user.reviewed_by_user_id = reviewer.id if reviewer is not None else None
    user.reviewed_at = datetime.now(UTC)
    session.flush()
    _ensure_home_workspace(session, user, quota_minutes=quota_minutes, quota_bytes=quota_bytes)
    claim_pending(session, user)
    return user


def _ensure_home_workspace(
    session: Session,
    user: User,
    *,
    quota_minutes: int | None = None,
    quota_bytes: int | None = None,
) -> Workspace | None:
    """이 사람이 쓸 워크스페이스가 하나도 없으면 개인 워크스페이스를 만들어 준다."""
    joined = session.scalar(
        select(func.count(WorkspaceMember.id)).where(WorkspaceMember.user_id == user.id)
    )
    if joined:
        if user.default_workspace_id is None:
            user.default_workspace_id = session.scalar(
                select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id)
            )
            session.flush()
        return None
    workspace = create_workspace(
        session,
        slug=unique_slug(session, user.email.split("@")[0]),
        name=f"{user.display_name}의 보관함",
        kind="personal",
        quota_minutes=quota_minutes,
        quota_bytes=quota_bytes,
    )
    add_member(session, workspace, user, "owner")
    user.default_workspace_id = workspace.id
    session.flush()
    return workspace


def reject(session: Session, user: User, *, reviewer: User | None = None) -> User:
    """거절. 그 사람의 개인 워크스페이스도 함께 지운다.

    안 지우면 거절된 계정의 빈 워크스페이스가 영구히 쌓이고, 워크스페이스를 지우는
    수단이 따로 없어 치울 곳이 없다. 녹음이 남아 있으면 지우지 않는다 — 대기 중에도
    업로드가 가능했다면 그건 다른 문제이므로 조용히 없애지 않는다.
    """
    user.status = "rejected"
    user.reviewed_by_user_id = reviewer.id if reviewer is not None else None
    user.reviewed_at = datetime.now(UTC)
    session.flush()
    _drop_empty_personal_workspaces(session, user)
    return user


def _drop_empty_personal_workspaces(session: Session, user: User) -> int:
    """이 사람만 있고 녹음도 없는 개인 워크스페이스를 지운다."""
    workspace_ids = session.scalars(
        select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id)
    ).all()
    dropped = 0
    for workspace_id in workspace_ids:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None or workspace.kind != "personal":
            continue
        members = session.scalar(
            select(func.count(WorkspaceMember.id)).where(
                WorkspaceMember.workspace_id == workspace_id
            )
        )
        recordings = session.scalar(
            select(func.count(Recording.id)).where(Recording.workspace_id == workspace_id)
        )
        if members == 1 and recordings == 0:
            if user.default_workspace_id == workspace_id:
                user.default_workspace_id = None
            session.execute(delete(Workspace).where(Workspace.id == workspace_id))
            dropped += 1
    session.flush()
    return dropped


def claim_pending(session: Session, user: User) -> tuple[int, int]:
    """가입 전에 이메일로 걸어둔 공유와 초대를 이 계정에 잇는다."""
    shares = session.scalars(
        select(RecordingShare).where(RecordingShare.invite_email == user.email)
    ).all()
    for share in shares:
        existing = session.scalar(
            select(RecordingShare).where(
                RecordingShare.recording_id == share.recording_id,
                RecordingShare.user_id == user.id,
            )
        )
        if existing is None:
            share.user_id = user.id
            share.invite_email = None
        else:
            # 이미 직접 공유받았으면 더 높은 쪽을 남기고 예약분은 지운다
            if share.permission == "edit":
                existing.permission = "edit"
            session.delete(share)

    invites = session.scalars(
        select(Invite).where(Invite.email == user.email, Invite.revoked_at.is_(None))
    ).all()
    joined = 0
    now = datetime.now(UTC)
    for invite in invites:
        if invite.expires_at is not None and invite.expires_at <= now:
            continue
        if invite.uses >= invite.max_uses:
            continue
        if _join_workspace(session, invite.workspace_id, user, invite.role):
            invite.uses += 1
            joined += 1
    session.flush()
    return len(shares), joined


def _join_workspace(session: Session, workspace_id: int, user: User, role: str) -> bool:
    already = session.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user.id,
        )
    )
    if already is not None:
        return False
    session.add(WorkspaceMember(workspace_id=workspace_id, user_id=user.id, role=role))
    session.flush()
    return True


@dataclass(frozen=True)
class IssuedInvite:
    invite: Invite
    token: str


def create_invite(
    session: Session,
    *,
    workspace: Workspace,
    role: str = "member",
    email: str | None = None,
    created_by: User | None = None,
    expires_in_days: int | None = INVITE_DEFAULT_DAYS,
    max_uses: int = 1,
) -> IssuedInvite:
    """구성원 초대. 원문 토큰은 여기서 한 번만 나온다.

    입력 검증을 라우트가 아니라 여기서 한다. 라우트에만 두면 CLI 경로가 같은 값을
    그대로 DB까지 내려보내고, 검사 제약이 터지며 500이 된다.
    """
    if role not in INVITABLE_ROLES:
        raise MemberInvalid(f"초대 역할은 {', '.join(INVITABLE_ROLES)} 중 하나여야 합니다")
    if expires_in_days is not None and not 1 <= expires_in_days <= INVITE_MAX_DAYS:
        raise MemberInvalid(f"만료 기간은 1일에서 {INVITE_MAX_DAYS}일 사이여야 합니다")
    if max_uses < 1:
        raise MemberInvalid("사용 횟수는 1 이상이어야 합니다")
    raw = new_token()
    now = datetime.now(UTC)
    invite = Invite(
        token_hash=token_hash(raw),
        email=normalize_email(email) if email else None,
        workspace_id=workspace.id,
        role=role,
        created_by_user_id=created_by.id if created_by is not None else None,
        expires_at=now + timedelta(days=expires_in_days) if expires_in_days else None,
        max_uses=max_uses,
    )
    session.add(invite)
    session.flush()
    return IssuedInvite(invite=invite, token=raw)


def accept_invite(session: Session, raw_token: str, user: User) -> Workspace:
    """초대를 받아 구성원이 된다. 없거나 만료·철회·소진된 초대는 구분하지 않는다.

    미리보기와 같은 판정을 쓴다. 두 벌로 두면 한쪽만 고쳐지고, 실제로 미리보기가
    이메일 검사를 빠뜨린 채 워크스페이스 이름을 내주고 있었다.
    """
    invite = peek_invite(session, raw_token, user)
    workspace = session.get(Workspace, invite.workspace_id)
    if workspace is None:
        raise InviteInvalid("초대가 유효하지 않습니다")
    # 이미 구성원이면 초대를 쓰지 않는다. 한 번짜리 초대를 구성원이 눌러 소모하면
    # 정작 부르려던 사람이 못 들어온다
    if _join_workspace(session, invite.workspace_id, user, invite.role):
        invite.uses += 1
    session.flush()
    return workspace


def bootstrap(
    session: Session,
    *,
    email: str,
    password: str,
    display_name: str,
    workspace_slug: str,
    workspace_name: str,
) -> tuple[User, Workspace, bool]:
    """첫 운영자와 그의 워크스페이스. 승인할 사람이 먼저 있어야 한다.

    마이그레이션은 행을 넣지 않으므로 이 자리가 유일한 출발점이다. 한도는 비워 둔다 —
    소유자의 백로그가 자기 한도에 걸리면 안 된다.
    """
    existing = find_user(session, email)
    if existing is not None:
        workspace = find_workspace(session, workspace_slug)
        if workspace is None:
            raise ValueError(f"{email}은 있는데 워크스페이스 {workspace_slug}가 없습니다")
        return existing, workspace, False

    workspace = find_workspace(session, workspace_slug)
    if workspace is None:
        workspace = create_workspace(session, slug=workspace_slug, name=workspace_name)
    user = create_user(
        session,
        email=email,
        password_hash=hash_password(password),
        display_name=display_name,
        status="active",
        is_service_admin=True,
    )
    add_member(session, workspace, user, "owner")
    user.default_workspace_id = workspace.id
    session.flush()
    return user, workspace, True


def list_members(session: Session, workspace: Workspace) -> list[tuple[WorkspaceMember, User]]:
    rows = session.execute(
        select(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == workspace.id)
        .order_by(WorkspaceMember.id)
    ).all()
    return [(member, user) for member, user in rows]


def find_member(session: Session, workspace: Workspace, user: User) -> WorkspaceMember | None:
    return session.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == user.id,
        )
    )


def set_member_role(
    session: Session, workspace: Workspace, user: User, role: str
) -> WorkspaceMember:
    """역할을 바꾼다. 소유자는 이 길로 만들지도 없애지도 못한다.

    소유권 이전은 범위 밖이다. 여기서 `owner`를 허용하면 부분 유니크 인덱스가
    IntegrityError로 거절하는데, 그건 사용자에게 500으로 보인다.
    """
    if role not in WORKSPACE_ROLES:
        raise MemberInvalid(f"역할은 {', '.join(WORKSPACE_ROLES)} 중 하나여야 합니다")
    if role == "owner":
        raise MemberInvalid("소유권 이전은 아직 지원하지 않습니다")
    member = find_member(session, workspace, user)
    if member is None:
        raise MemberInvalid("이 워크스페이스의 구성원이 아닙니다")
    if member.role == "owner":
        raise MemberInvalid("소유자의 역할은 바꿀 수 없습니다")
    member.role = role
    session.flush()
    return member


def remove_member(session: Session, workspace: Workspace, user: User) -> None:
    """구성원을 뺀다. 녹음은 워크스페이스 것이므로 그대로 남는다.

    이 사람 앞으로 걸려 있던 초대는 함께 철회한다. 남겨 두면 방금 뺀 사람이 같은
    토큰으로 다시 들어온다. 이메일을 지정하지 않은 초대는 건드리지 않는다 — 그건
    특정인이 아니라 누구에게나 열어 둔 자리다.
    """
    member = find_member(session, workspace, user)
    if member is None:
        raise MemberInvalid("이 워크스페이스의 구성원이 아닙니다")
    if member.role == "owner":
        raise MemberInvalid("소유자는 뺄 수 없습니다")
    session.delete(member)
    now = datetime.now(UTC)
    for invite in session.scalars(
        select(Invite).where(
            Invite.workspace_id == workspace.id,
            Invite.email == user.email,
            Invite.revoked_at.is_(None),
        )
    ):
        invite.revoked_at = now
    session.flush()
    if user.default_workspace_id == workspace.id:
        # 기본 워크스페이스가 없어진 사람이 로그인하면 콘솔이 어디로 갈지 모른다
        user.default_workspace_id = session.scalar(
            select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id)
        )
        session.flush()


def list_invites(session: Session, workspace: Workspace) -> list[Invite]:
    """살아있는 초대만. 철회·소진된 것은 화면에 남을 이유가 없다."""
    now = datetime.now(UTC)
    rows = session.scalars(
        select(Invite)
        .where(Invite.workspace_id == workspace.id, Invite.revoked_at.is_(None))
        .order_by(Invite.id)
    ).all()
    return [
        row
        for row in rows
        if row.uses < row.max_uses and (row.expires_at is None or row.expires_at > now)
    ]


def revoke_invite(session: Session, workspace: Workspace, public_id) -> bool:
    """철회. 워크스페이스로 범위를 좁혀 찾는다 — 남의 초대 id로는 아무것도 못 지운다."""
    invite = session.scalar(
        select(Invite).where(
            Invite.workspace_id == workspace.id,
            Invite.public_id == public_id,
            Invite.revoked_at.is_(None),
        )
    )
    if invite is None:
        return False
    invite.revoked_at = datetime.now(UTC)
    session.flush()
    return True


def peek_invite(
    session: Session, raw_token: str, user: User, *, now: datetime | None = None
) -> Invite:
    """받는 사람 화면이 "어디로 부르는 초대인지"를 그리기 위한 조회.

    **받을 사람을 `accept_invite`와 같은 규칙으로 검사한다.** 여기서 빠뜨리면 이메일을
    지정한 초대의 요지("그 사람만")가 미리보기에서 무너진다 — 대상이 아닌 사람이
    토큰만 쥐고 워크스페이스 이름을 얻는다. 수락은 막히는데 이름은 새는 상태다.
    """
    invite = _live_invite(session, raw_token, now=now)
    if invite.email is not None and invite.email != user.email:
        raise InviteInvalid("초대가 유효하지 않습니다")
    return invite


def _live_invite(session: Session, raw_token: str, *, now: datetime | None = None) -> Invite:
    """없음·만료·철회·소진을 구분해서 알려주지 않는다."""
    now = now or datetime.now(UTC)
    invite = session.scalar(select(Invite).where(Invite.token_hash == token_hash(raw_token)))
    if (
        invite is None
        or invite.revoked_at is not None
        or (invite.expires_at is not None and invite.expires_at <= now)
        or invite.uses >= invite.max_uses
    ):
        raise InviteInvalid("초대가 유효하지 않습니다")
    return invite
