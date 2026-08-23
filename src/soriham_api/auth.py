"""비밀번호와 토큰, 그리고 로그인 세션.

**비밀번호만 느린 해시(argon2id)를 쓴다.** 세션·초대·공유 링크 토큰은 우리가 만든
256비트 난수라 사전 공격이 성립하지 않고, 매 요청 1회 조회가 인덱스 동등 탐색이어야
한다 — 솔트 해시는 인덱싱이 불가능해 풀스캔이 된다. 그래도 저장은 해시로 한다:
백업이나 로그가 새도 그것이 곧바로 살아있는 세션이 되면 안 된다.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import User, UserSession

# 기본값(time_cost=3, memory_cost=64MiB, parallelism=4)을 그대로 쓴다. 메모리 사용량이
# 곧 공격 표면이기도 하므로 로그인·가입 시도 제한이 이 호출보다 앞에 있어야 한다
_hasher = PasswordHasher()

# 존재하지 않는 계정에도 같은 시간을 쓰기 위한 표적. 모듈 로드 때 한 번만 만든다
_DUMMY_HASH = _hasher.hash("존재하지 않는 계정을 위한 자리")

TOKEN_BYTES = 32
SESSION_IDLE_DAYS = 14
SESSION_ABSOLUTE_DAYS = 90
# 읽기 경로에서 매번 쓰지 않기 위한 간격. 세션 갱신은 사용자가 못 느낄 정도면 충분하다
SESSION_TOUCH_INTERVAL = timedelta(hours=1)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """비밀번호를 확인한다. 계정이 없어도 같은 시간을 쓴다.

    `stored_hash`가 None이면(계정 없음) 더미 해시로 검증을 돌린다. 이게 없으면 응답
    시간이 "그 이메일이 있는가"를 알려주는 탐침이 된다.
    """
    target = stored_hash if stored_hash is not None else _DUMMY_HASH
    try:
        _hasher.verify(target, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return stored_hash is not None


def needs_rehash(stored_hash: str) -> bool:
    """파라미터가 올라간 뒤 로그인한 계정을 조용히 따라 올리기 위한 것."""
    return _hasher.check_needs_rehash(stored_hash)


def new_token() -> str:
    """추측 불가한 토큰 원문. 발급 응답에 한 번만 실리고 저장하지 않는다."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(raw: str) -> str:
    """저장·조회에 쓰는 형태. 고엔트로피 난수라 sha256으로 충분하다."""
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class IssuedSession:
    """세션 발급 결과. 원문 토큰은 여기서만 나온다."""

    session: UserSession
    token: str
    csrf_token: str


def create_session(
    db: Session,
    user: User,
    *,
    user_agent: str | None = None,
    ip: str | None = None,
    now: datetime | None = None,
) -> IssuedSession:
    now = now or datetime.now(UTC)
    raw = new_token()
    csrf = new_token()
    absolute = now + timedelta(days=SESSION_ABSOLUTE_DAYS)
    session = UserSession(
        user_id=user.id,
        token_hash=token_hash(raw),
        csrf_token=csrf,
        issued_at=now,
        last_seen_at=now,
        expires_at=min(now + timedelta(days=SESSION_IDLE_DAYS), absolute),
        absolute_expires_at=absolute,
        user_agent=user_agent,
        ip=ip,
    )
    db.add(session)
    db.flush()
    return IssuedSession(session=session, token=raw, csrf_token=csrf)


def load_session(db: Session, raw_token: str, *, now: datetime | None = None) -> UserSession | None:
    """원문 토큰으로 살아있는 세션을 찾는다. 만료·폐기된 것은 없는 것과 같다."""
    now = now or datetime.now(UTC)
    session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(raw_token)))
    if session is None or session.revoked_at is not None:
        return None
    if session.expires_at <= now or session.absolute_expires_at <= now:
        return None
    return session


def touch_session(db: Session, session: UserSession, *, now: datetime | None = None) -> bool:
    """유휴 만료를 뒤로 민다. 절대 만료는 넘지 않는다.

    간격을 두는 이유: 읽기 요청마다 쓰기가 붙으면 오디오 스트리밍처럼 잦은 GET이
    그대로 쓰기 부하가 된다.
    """
    now = now or datetime.now(UTC)
    if now - session.last_seen_at < SESSION_TOUCH_INTERVAL:
        return False
    session.last_seen_at = now
    session.expires_at = min(now + timedelta(days=SESSION_IDLE_DAYS), session.absolute_expires_at)
    db.flush()
    return True


def revoke_session(db: Session, session: UserSession, *, now: datetime | None = None) -> None:
    session.revoked_at = now or datetime.now(UTC)
    db.flush()


def revoke_other_sessions(
    db: Session, user: User, *, keep: UserSession | None = None, now: datetime | None = None
) -> int:
    """비밀번호가 바뀌면 다른 자리의 세션은 전부 죽어야 한다."""
    now = now or datetime.now(UTC)
    rows = db.scalars(
        select(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
    ).all()
    revoked = 0
    for row in rows:
        if keep is not None and row.id == keep.id:
            continue
        row.revoked_at = now
        revoked += 1
    db.flush()
    return revoked
