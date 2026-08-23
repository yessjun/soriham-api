"""녹음 하나를 워크스페이스 밖으로 여는 두 가지 길.

**사용자 지정 공유**는 사람에게 붙고, **공유 링크**는 토큰에 붙는다. 둘 다 소유권을
옮기지 않고 권한을 더할 뿐이라 [permissions.py](permissions.py)의 최댓값 해석 하나로
합쳐진다.

FastAPI를 임포트하지 않는다 — 규칙을 세션 하나로 시험할 수 있어야 라우트가 바뀌어도
그대로 남는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import new_token, token_hash
from .models import (
    SHARE_PERMISSIONS,
    Recording,
    RecordingShare,
    ShareLink,
    User,
)
from .tenancy import MemberInvalid, valid_email

LINK_DEFAULT_DAYS = 30
# 링크 비밀번호의 최소 길이. 잠금 해제는 출처와 링크의 짝으로 세므로 IP를 바꿔 가며
# 두드리면 제한을 우회할 수 있다. 짧은 비밀번호는 그 앞에서 버티지 못한다
LINK_PASSWORD_MIN = 6
# 사람이 손으로 넣는 값이라 상한을 둔다. 무기한은 일수가 아니라 None으로 말한다
LINK_MAX_DAYS = 3650


class ShareInvalid(Exception):
    """공유를 만들 수 없다. 메시지는 사용자에게 그대로 보여진다."""


class LinkInvalid(Exception):
    """링크가 없거나 만료·철회됐다. 셋을 구분해서 알려주지 않는다."""


def validate_permission(permission: str) -> str:
    if permission not in SHARE_PERMISSIONS:
        raise ShareInvalid(f"공유 권한은 {' 또는 '.join(SHARE_PERMISSIONS)}만 됩니다")
    return permission


def share_with_email(
    db: Session,
    recording: Recording,
    *,
    email: str,
    permission: str = "view",
    created_by: User | None = None,
) -> RecordingShare:
    """사람 하나에게 이 녹음을 연다. 아직 가입하지 않은 이메일도 받는다.

    가입 전 이메일로 걸어둔 공유는 승인 시 `tenancy.claim_pending`이 계정에 잇는다.
    미가입이라고 거절하면 "먼저 가입하라고 말한 다음 다시 공유"라는 절차가 생긴다.
    """
    validate_permission(permission)
    try:
        normalized = valid_email(email)
    except MemberInvalid as exc:
        raise ShareInvalid(str(exc)) from None

    target = db.scalar(select(User).where(User.email == normalized))
    if target is not None and created_by is not None and target.id == created_by.id:
        raise ShareInvalid("자기 자신에게는 공유할 수 없습니다")

    existing = _find_share(db, recording, user=target, email=normalized)
    if existing is not None:
        existing.permission = permission
        db.flush()
        return existing

    share = RecordingShare(
        recording_id=recording.id,
        user_id=target.id if target is not None else None,
        invite_email=None if target is not None else normalized,
        permission=permission,
        created_by_user_id=created_by.id if created_by is not None else None,
    )
    db.add(share)
    db.flush()
    return share


def _find_share(
    db: Session, recording: Recording, *, user: User | None, email: str
) -> RecordingShare | None:
    if user is not None:
        return db.scalar(
            select(RecordingShare).where(
                RecordingShare.recording_id == recording.id,
                RecordingShare.user_id == user.id,
            )
        )
    return db.scalar(
        select(RecordingShare).where(
            RecordingShare.recording_id == recording.id,
            RecordingShare.invite_email == email,
        )
    )


def list_shares(db: Session, recording: Recording) -> list[tuple[RecordingShare, User | None]]:
    rows = db.execute(
        select(RecordingShare, User)
        .outerjoin(User, User.id == RecordingShare.user_id)
        .where(RecordingShare.recording_id == recording.id)
        .order_by(RecordingShare.id)
    ).all()
    return [(share, user) for share, user in rows]


def revoke_share(db: Session, recording: Recording, public_id) -> bool:
    """철회. 녹음으로 범위를 좁혀 찾는다 — 남의 공유 id로는 아무것도 지우지 못한다."""
    share = db.scalar(
        select(RecordingShare).where(
            RecordingShare.recording_id == recording.id,
            RecordingShare.public_id == public_id,
        )
    )
    if share is None:
        return False
    db.delete(share)
    db.flush()
    return True


@dataclass(frozen=True)
class IssuedLink:
    """발급 결과. 원문 토큰은 여기서만 나온다."""

    link: ShareLink
    token: str


def create_link(
    db: Session,
    recording: Recording,
    *,
    label: str | None = None,
    password_hash: str | None = None,
    allow_audio: bool = True,
    allow_speaker_names: bool = True,
    expires_in_days: int | None = LINK_DEFAULT_DAYS,
    created_by: User | None = None,
    now: datetime | None = None,
) -> IssuedLink:
    now = now or datetime.now(UTC)
    if expires_in_days is not None and not 1 <= expires_in_days <= LINK_MAX_DAYS:
        raise ShareInvalid(f"만료 기간은 1일에서 {LINK_MAX_DAYS}일 사이여야 합니다")
    raw = new_token()
    link = ShareLink(
        recording_id=recording.id,
        token_hash=token_hash(raw),
        label=(label or "").strip() or None,
        password_hash=password_hash,
        allow_audio=allow_audio,
        allow_speaker_names=allow_speaker_names,
        expires_at=now + timedelta(days=expires_in_days) if expires_in_days else None,
        created_by_user_id=created_by.id if created_by is not None else None,
    )
    db.add(link)
    db.flush()
    return IssuedLink(link=link, token=raw)


def list_links(db: Session, recording: Recording) -> list[ShareLink]:
    """살아있는 링크만. 철회된 것은 화면에 남을 이유가 없다."""
    return list(
        db.scalars(
            select(ShareLink)
            .where(ShareLink.recording_id == recording.id, ShareLink.revoked_at.is_(None))
            .order_by(ShareLink.id)
        ).all()
    )


def revoke_link(
    db: Session, recording: Recording, public_id, *, now: datetime | None = None
) -> bool:
    link = db.scalar(
        select(ShareLink).where(
            ShareLink.recording_id == recording.id,
            ShareLink.public_id == public_id,
            ShareLink.revoked_at.is_(None),
        )
    )
    if link is None:
        return False
    # 행을 지우지 않고 도장을 찍는다 — 조회수와 마지막 열람 시각이 사고 조사 재료다
    link.revoked_at = now or datetime.now(UTC)
    db.flush()
    return True


def load_link(db: Session, raw_token: str, *, now: datetime | None = None) -> ShareLink:
    """원문 토큰으로 살아있는 링크를 찾는다.

    없음·만료·철회를 구분해서 알려주지 않는다. 구분하면 만료된 토큰을 가진 사람이
    "그런 녹음이 있긴 있었다"를 알게 된다.
    """
    now = now or datetime.now(UTC)
    link = db.scalar(select(ShareLink).where(ShareLink.token_hash == token_hash(raw_token)))
    if link is None or link.revoked_at is not None:
        raise LinkInvalid("링크가 유효하지 않습니다")
    if link.expires_at is not None and link.expires_at <= now:
        raise LinkInvalid("링크가 유효하지 않습니다")
    return link


def unlock_value(link: ShareLink) -> str:
    """잠금 해제 쿠키에 담을 값. 비밀번호를 아는 사람만 받을 수 있다.

    서명 키를 새로 두지 않고 저장된 해시에서 파생한다. 그러면 비밀번호를 바꿀 때
    이미 나간 쿠키가 저절로 죽고, 관리할 시크릿이 하나도 늘지 않는다.
    """
    if link.password_hash is None:
        raise ShareInvalid("비밀번호가 걸려 있지 않은 링크입니다")
    return token_hash(link.password_hash)


def is_unlocked(link: ShareLink, cookie_value: str | None) -> bool:
    """비밀번호가 없는 링크는 늘 열려 있다."""
    if link.password_hash is None:
        return True
    if not cookie_value:
        return False
    return cookie_value == unlock_value(link)


def record_view(db: Session, link: ShareLink, *, now: datetime | None = None) -> None:
    """조회수는 보안 신호가 아니라 근사치다 — 메신저 미리보기와 백신이 부풀린다."""
    link.view_count += 1
    link.last_viewed_at = now or datetime.now(UTC)
    db.flush()


@dataclass(frozen=True)
class ShareCounts:
    user_count: int
    link_count: int


def share_counts(db: Session, recording: Recording) -> ShareCounts:
    """상세 응답이 "이 녹음은 공유돼 있다"를 말하기 위한 값."""
    users = db.scalar(
        select(func.count(RecordingShare.id)).where(RecordingShare.recording_id == recording.id)
    )
    links = db.scalar(
        select(func.count(ShareLink.id)).where(
            ShareLink.recording_id == recording.id, ShareLink.revoked_at.is_(None)
        )
    )
    return ShareCounts(user_count=int(users or 0), link_count=int(links or 0))
