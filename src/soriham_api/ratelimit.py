"""로그인·가입·링크 잠금 해제의 시도 제한.

가입 폼이 인터넷에 열려 있고 그 뒤에 argon2가 있다. argon2는 한 번에 64MiB를 쓰므로
해싱보다 먼저 막지 않으면 시도 제한이 아니라 메모리를 고갈시키는 길이 열린다.

세는 축은 출처(IP)와 (출처, 대상) 짝 두 가지다. 앞은 한 곳에서 쏟아지는 시도를,
뒤는 그 IP가 특정 계정이나 링크 하나를 두드리는 것을 막는다.

대상만으로는 세지 않는다. 이메일 하나를 전역으로 잠그면 남의 계정을 임의로 잠그는
수단이 된다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .models import AuthAttempt


@dataclass(frozen=True)
class Limit:
    max_attempts: int
    window: timedelta

    def start_of(self, now: datetime) -> datetime:
        """고정 창의 시작점. 창이 지나면 카운터가 저절로 새 행으로 넘어간다."""
        seconds = int(self.window.total_seconds())
        epoch = int(now.timestamp())
        return datetime.fromtimestamp(epoch - epoch % seconds, tz=UTC)


WINDOW = timedelta(minutes=15)
# 한 출처가 쏟아붓는 것을 막는 값. 사람이 쓰다가 닿을 수는 없는 높이여야 한다
PER_SOURCE = Limit(max_attempts=30, window=WINDOW)
# 그 출처가 계정 하나 또는 링크 하나를 두드리는 것
PER_TARGET = Limit(max_attempts=10, window=WINDOW)

# 창 하나가 지나고도 남은 행은 아무 의미가 없다
SWEEP_AFTER = WINDOW * 4


class TooManyAttempts(Exception):
    """시도가 너무 잦다. 메시지는 사용자에게 그대로 보여진다."""


def source_key(kind: str, ip: str | None) -> str:
    return f"{kind}:src:{ip or 'unknown'}"


def target_key(kind: str, ip: str | None, target: str) -> str:
    """대상은 해시로 넣는다. 이 표가 이메일과 링크 토큰 목록이 되면 안 된다."""
    digest = hashlib.sha256(target.encode()).hexdigest()[:32]
    return f"{kind}:pair:{ip or 'unknown'}:{digest}"


def hit(db: Session, key: str, *, limit: Limit, now: datetime | None = None) -> int:
    """카운터를 하나 올리고 올린 값을 돌려준다.

    읽고 나서 쓰면 동시 요청이 서로의 증가를 덮어써 제한이 헐거워진다. 삽입과 증가를
    한 문장으로 묶어 DB가 직렬화하게 한다.
    """
    now = now or datetime.now(UTC)
    stmt = (
        insert(AuthAttempt)
        .values(key=key, window_start=limit.start_of(now), count=1)
        .on_conflict_do_update(
            constraint="uq_auth_attempts_key_window",
            set_={"count": AuthAttempt.__table__.c.count + 1},
        )
        .returning(AuthAttempt.count)
    )
    return int(db.scalar(stmt) or 1)


def clear(db: Session, key: str) -> None:
    """성공했으면 그 축의 기록을 지운다.

    실패만 쌓이게 하려는 것이다. 성공까지 세면 자기 비밀번호를 아는 사람이 정상적으로
    드나들다가 막힌다.
    """
    db.execute(delete(AuthAttempt).where(AuthAttempt.key == key))


def guard(db: Session, keys: list[tuple[str, Limit]], *, now: datetime | None = None) -> None:
    """축을 한꺼번에 올리고 하나라도 넘으면 막는다.

    창은 고정이다. 넘긴 채로 계속 두드려도 다음 창은 0에서 다시 시작한다 — 잠그는
    시간을 늘리지 않는 것은 남의 계정이나 링크를 무기한 잠그는 수단을 만들지 않기
    위해서다. 어느 축에서 걸렸는지는 알려주지 않는다.
    """
    over = False
    for key, limit in keys:
        if hit(db, key, limit=limit, now=now) > limit.max_attempts:
            over = True
    if over:
        raise TooManyAttempts("시도가 너무 잦습니다. 잠시 후 다시 시도해 주세요")


def sweep(db: Session, *, now: datetime | None = None) -> int:
    """지난 창의 행을 치운다. 안 치우면 이 표만 끝없이 자란다."""
    now = now or datetime.now(UTC)
    result = db.execute(delete(AuthAttempt).where(AuthAttempt.window_start < now - SWEEP_AFTER))
    return result.rowcount or 0


def count_of(db: Session, key: str, *, limit: Limit, now: datetime | None = None) -> int:
    """지금 창의 값. 시험과 진단용이다."""
    now = now or datetime.now(UTC)
    value = db.scalar(
        select(AuthAttempt.count).where(
            AuthAttempt.key == key, AuthAttempt.window_start == limit.start_of(now)
        )
    )
    return int(value or 0)
