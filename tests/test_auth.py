from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from soriham_api import auth
from soriham_api.models import User, UserSession
from soriham_api.tenancy import create_user


@pytest.fixture
def user(db):
    u = create_user(
        db,
        email="Someone@Example.COM",
        password_hash=auth.hash_password("바른 말 열쇠 여덟"),
        display_name="아무개",
        status="active",
    )
    db.commit()
    return u


def test_이메일은_소문자로_정규화돼_저장된다(db, user):
    """스키마에 lower() CHECK가 있어서 정규화를 빠뜨리면 INSERT가 터진다."""
    assert user.email == "someone@example.com"


def test_비밀번호가_맞아야_통과한다(user):
    assert auth.verify_password(user.password_hash, "바른 말 열쇠 여덟")
    assert not auth.verify_password(user.password_hash, "틀린 말")


def test_없는_계정도_같은_방식으로_거절한다(user):
    """None을 그냥 False로 돌려보내면 응답 시간이 계정 존재를 알려준다."""
    assert auth.verify_password(None, "아무 말") is False


def test_긴_한글_암구호도_끝까지_구분한다(db):
    """bcrypt를 쓰지 않는 이유. 72바이트를 넘는 지점에서 조용히 잘리면 안 된다."""
    base = "한글 암구호는 스물네 글자면 일흔두 바이트를 넘는다 그래서"
    stored = auth.hash_password(base + "앞")
    assert auth.verify_password(stored, base + "앞")
    assert not auth.verify_password(stored, base + "뒤")


def test_같은_비밀번호도_해시가_매번_다르다(db):
    assert auth.hash_password("같은 말") != auth.hash_password("같은 말")


def test_토큰은_매번_다르고_해시는_같은_값에_고정이다():
    a, b = auth.new_token(), auth.new_token()
    assert a != b
    assert auth.token_hash(a) == auth.token_hash(a)
    assert auth.token_hash(a) != auth.token_hash(b)


def test_DB에는_원문_토큰이_남지_않는다(db, user):
    issued = auth.create_session(db, user)
    db.commit()
    rows = db.scalars(select(UserSession)).all()
    assert all(row.token_hash != issued.token for row in rows)
    assert rows[0].token_hash == auth.token_hash(issued.token)


def test_원문_토큰으로_세션을_찾는다(db, user):
    issued = auth.create_session(db, user)
    db.commit()
    found = auth.load_session(db, issued.token)
    assert found is not None and found.id == issued.session.id
    assert auth.load_session(db, auth.new_token()) is None


def test_만료된_세션은_없는_것과_같다(db, user):
    issued = auth.create_session(db, user)
    issued.session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert auth.load_session(db, issued.token) is None


def test_처음_발급된_세션은_유휴_기간만큼만_산다(db, user):
    """절대 만료(90일)를 유휴 만료로 잘못 쓰면 손대지 않은 세션이 석 달을 산다."""
    now = datetime.now(UTC)
    issued = auth.create_session(db, user, now=now)
    db.commit()
    assert issued.session.expires_at == now + timedelta(days=auth.SESSION_IDLE_DAYS)
    assert issued.session.absolute_expires_at == now + timedelta(days=auth.SESSION_ABSOLUTE_DAYS)


def test_절대_만료가_지났으면_유휴_기한이_남아도_죽는다(db, user):
    """발급·갱신이 늘 절대 만료로 잘라주지만, 그 불변식이 깨져도 조회에서 걸러야 한다."""
    now = datetime.now(UTC)
    issued = auth.create_session(db, user, now=now)
    issued.session.absolute_expires_at = now - timedelta(seconds=1)
    db.commit()
    assert auth.load_session(db, issued.token) is None


def test_절대_만료는_유휴_갱신으로_넘지_못한다(db, user):
    """오래 켜둔 세션이 영원히 사는 것을 막는 한계선이다."""
    now = datetime.now(UTC)
    issued = auth.create_session(db, user, now=now)
    issued.session.absolute_expires_at = now + timedelta(minutes=5)
    db.commit()

    auth.touch_session(db, issued.session, now=now + timedelta(hours=2))
    assert issued.session.expires_at == issued.session.absolute_expires_at


def test_유휴_갱신은_간격을_두고_한다(db, user):
    """읽기마다 쓰기가 붙으면 오디오 스트리밍이 그대로 쓰기 부하가 된다."""
    now = datetime.now(UTC)
    issued = auth.create_session(db, user, now=now)
    db.commit()

    assert auth.touch_session(db, issued.session, now=now + timedelta(minutes=5)) is False
    assert auth.touch_session(db, issued.session, now=now + timedelta(hours=2)) is True


def test_폐기한_세션은_다시_쓸_수_없다(db, user):
    issued = auth.create_session(db, user)
    db.commit()
    auth.revoke_session(db, issued.session)
    db.commit()
    assert auth.load_session(db, issued.token) is None


def test_비밀번호를_바꾸면_다른_자리의_세션이_죽는다(db, user):
    here = auth.create_session(db, user)
    there = auth.create_session(db, user)
    db.commit()

    revoked = auth.revoke_other_sessions(db, user, keep=here.session)
    db.commit()

    assert revoked == 1
    assert auth.load_session(db, here.token) is not None
    assert auth.load_session(db, there.token) is None


def test_남의_세션은_건드리지_않는다(db, user):
    other = create_user(
        db,
        email="other@example.com",
        password_hash=auth.hash_password("다른 말"),
        display_name="다른 사람",
        status="active",
    )
    mine = auth.create_session(db, user)
    theirs = auth.create_session(db, other)
    db.commit()

    auth.revoke_other_sessions(db, user)
    db.commit()

    assert auth.load_session(db, mine.token) is None
    assert auth.load_session(db, theirs.token) is not None


def test_세션마다_다른_csrf_토큰을_받는다(db, user):
    a = auth.create_session(db, user)
    b = auth.create_session(db, user)
    db.commit()
    assert a.csrf_token != b.csrf_token


def test_대기_상태로도_계정은_만들어진다(db):
    """승인제 — 가입은 되고 관문은 상태다."""
    u = create_user(
        db,
        email="pending@example.com",
        password_hash=auth.hash_password("암구호"),
        display_name="신청자",
    )
    db.commit()
    assert db.scalar(select(User.status).where(User.id == u.id)) == "pending"


def test_죽은_세션은_치운다(db, user):
    """안 치우면 이 표가 로그인 횟수만큼 자란다."""
    from datetime import timedelta

    from soriham_api.auth import SESSION_KEEP_AFTER, sweep_sessions
    from soriham_api.models import UserSession

    live = auth.create_session(db, user).session
    old = auth.create_session(db, user).session
    old.expires_at = datetime.now(UTC) - SESSION_KEEP_AFTER - timedelta(days=1)
    old.absolute_expires_at = old.expires_at
    db.commit()

    assert sweep_sessions(db) == 1
    remaining = db.scalars(select(UserSession.id)).all()
    assert remaining == [live.id]


def test_아직_안_지난_세션은_남긴다(db, user):
    """만료되자마자 지우면 "언제 어디서 로그인했나"를 볼 수 없다."""
    from datetime import timedelta

    from soriham_api.auth import sweep_sessions
    from soriham_api.models import UserSession

    just_expired = auth.create_session(db, user).session
    just_expired.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db.commit()

    assert sweep_sessions(db) == 0
    assert db.scalars(select(UserSession.id)).all() == [just_expired.id]
