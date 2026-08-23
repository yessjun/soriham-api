"""시도 제한. 가입 폼이 인터넷에 열려 있고 그 뒤에 argon2가 있다."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from soriham_api.models import AuthAttempt
from soriham_api.ratelimit import (
    PER_SOURCE,
    PER_TARGET,
    Limit,
    TooManyAttempts,
    clear,
    count_of,
    guard,
    hit,
    source_key,
    sweep,
    target_key,
)

SMALL = Limit(max_attempts=3, window=timedelta(minutes=15))


@pytest.fixture
def 창_고정(monkeypatch: pytest.MonkeyPatch):
    """고정 창은 매시 :00, :15, :30, :45에 새 행으로 넘어간다.

    한도까지 두드리는 시험이 그 경계를 밟으면 카운터가 리셋돼 마지막 요청이 통과한다.
    argon2가 한 번에 수십 밀리초라 30여 번을 두드리는 데 몇 초가 걸리고, 실제로 CI가
    20:00:00을 밟아 빨갛게 났다.
    """
    frozen = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(Limit, "start_of", lambda self, now: frozen)


def test_올린_만큼_센다(db):
    assert [hit(db, "k", limit=SMALL) for _ in range(3)] == [1, 2, 3]
    assert count_of(db, "k", limit=SMALL) == 3


def test_한도를_넘으면_막는다(db):
    for _ in range(3):
        guard(db, [("k", SMALL)])
    with pytest.raises(TooManyAttempts):
        guard(db, [("k", SMALL)])


def test_막힌_뒤에도_계속_센다(db):
    """세는 것 자체는 멈추지 않는다. 다만 창이 고정이라 다음 창은 0에서 시작한다."""
    for _ in range(3):
        guard(db, [("k", SMALL)])
    for _ in range(5):
        with pytest.raises(TooManyAttempts):
            guard(db, [("k", SMALL)])

    assert count_of(db, "k", limit=SMALL) == 8


def test_창이_바뀌면_새로_센다(db):
    now = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    for _ in range(3):
        hit(db, "k", limit=SMALL, now=now)

    assert hit(db, "k", limit=SMALL, now=now + timedelta(minutes=16)) == 1


def test_성공하면_그_축이_지워진다(db):
    """실패만 쌓여야 한다. 성공까지 세면 정상 사용자가 드나들다가 막힌다."""
    for _ in range(3):
        hit(db, "k", limit=SMALL)
    clear(db, "k")

    assert count_of(db, "k", limit=SMALL) == 0


def test_동시_요청에서도_증가가_새지_않는다(engine, db):
    """읽고 나서 쓰면 두 요청이 서로의 증가를 덮어써 제한이 헐거워진다."""
    now = datetime.now(UTC)
    with Session(engine) as a, Session(engine) as b:
        hit(a, "동시", limit=SMALL, now=now)
        a.commit()
        hit(b, "동시", limit=SMALL, now=now)
        b.commit()

    assert count_of(db, "동시", limit=SMALL, now=now) == 2


def test_남의_이메일로_그_사람을_잠글_수_없다(db):
    """대상만으로 세면 남의 계정을 마음대로 잠그는 수단이 된다."""
    victim = "victim@example.com"
    attacker_keys = [target_key("login", "10.0.0.1", victim) for _ in range(20)]
    for key in attacker_keys:
        hit(db, key, limit=PER_TARGET)

    # 피해자는 다른 출처에서 들어온다 — 공격자의 카운터와 겹치지 않아야 한다
    guard(db, [(target_key("login", "10.0.0.2", victim), PER_TARGET)])


def test_출처가_같으면_대상을_바꿔도_막힌다(db):
    """계정을 갈아가며 두드리는 것은 출처 축이 잡는다."""
    ip = "10.0.0.9"
    for _ in range(PER_SOURCE.max_attempts):
        guard(db, [(source_key("login", ip), PER_SOURCE)])

    with pytest.raises(TooManyAttempts):
        guard(db, [(source_key("login", ip), PER_SOURCE)])


def test_대상은_원문으로_저장되지_않는다(db):
    """이 표가 이메일과 링크 토큰 목록이 되면 안 된다."""
    hit(db, target_key("login", "10.0.0.1", "secret@example.com"), limit=PER_TARGET)

    keys = db.scalars(select(AuthAttempt.key)).all()
    assert all("secret@example.com" not in k for k in keys)


def test_출처를_모르면_한_바구니로_센다(db):
    """IP를 못 읽는 배치에서 제한이 통째로 사라지면 안 된다."""
    assert source_key("login", None) == source_key("login", None)


def test_지난_창은_치운다(db):
    now = datetime.now(UTC)
    hit(db, "옛것", limit=SMALL, now=now - timedelta(hours=5))
    hit(db, "지금", limit=SMALL, now=now)

    assert sweep(db, now=now) == 1
    assert db.scalar(select(func.count(AuthAttempt.id))) == 1


# --- 라우트에 실제로 걸려 있는가 -----------------------------------------------


@pytest.fixture
def client(engine, db, owner):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from conftest import make_settings
    from soriham_api.app import create_app

    return TestClient(
        create_app(settings=make_settings(), session_factory=sessionmaker(bind=engine))
    )


def test_로그인을_두드리면_막힌다(client, owner, 창_고정):
    from conftest import TEST_PASSWORD

    codes = []
    for _ in range(PER_TARGET.max_attempts + 2):
        codes.append(
            client.post(
                "/api/auth/login", json={"email": owner.email, "password": "틀린 암구호"}
            ).status_code
        )

    assert codes[0] == 401
    assert 429 in codes
    # 막힌 뒤에는 맞는 비밀번호도 통하지 않는다 — 그래야 두드리기가 의미를 잃는다
    blocked = client.post("/api/auth/login", json={"email": owner.email, "password": TEST_PASSWORD})
    assert blocked.status_code == 429


def test_성공하면_카운터가_지워진다(client, db, owner):
    from conftest import TEST_PASSWORD

    for _ in range(PER_TARGET.max_attempts - 1):
        client.post("/api/auth/login", json={"email": owner.email, "password": "틀린 암구호"})
    assert (
        client.post(
            "/api/auth/login", json={"email": owner.email, "password": TEST_PASSWORD}
        ).status_code
        == 200
    )

    # 다시 실패해도 아까 쌓인 것 때문에 바로 막히면 안 된다
    again = client.post("/api/auth/login", json={"email": owner.email, "password": "틀린 암구호"})
    assert again.status_code == 401


def test_막을_때도_카운터가_남는다(client, db, owner):
    """요청 세션에 얹으면 예외의 롤백에 증가가 함께 쓸려 나가 영원히 안 막힌다."""
    for _ in range(3):
        client.post("/api/auth/login", json={"email": owner.email, "password": "틀린 암구호"})

    assert db.scalar(select(func.sum(AuthAttempt.count))) >= 3


def test_가입도_두드리면_막힌다(client, 창_고정):
    codes = []
    for i in range(PER_SOURCE.max_attempts + 2):
        codes.append(
            client.post(
                "/api/auth/signup",
                json={"email": f"x{i}@example.com", "password": "암구호", "display_name": "x"},
            ).status_code
        )

    assert codes[0] == 201
    assert codes[-1] == 429


def test_링크_비밀번호도_두드리면_막힌다(client, db, workspace, owner, 창_고정):
    from conftest import login
    from soriham_api.models import Recording

    rec = Recording(
        workspace_id=workspace.id,
        source="upload",
        path="/tmp/x/a.wav",
        filename="a.wav",
        size_bytes=10,
        partial_hash="rl-a",
        status="done",
    )
    db.add(rec)
    db.commit()
    login(client, owner.email)
    token = client.post(
        f"/api/recordings/{rec.public_id}/links", json={"password": "열려라 참깨"}
    ).json()["token"]

    codes = [
        client.post(f"/api/shared/{token}/unlock", json={"password": "틀림"}).status_code
        for _ in range(PER_TARGET.max_attempts + 2)
    ]

    assert codes[0] == 403
    assert codes[-1] == 429


def test_워커의_유휴_정리가_지난_기록을_치운다(engine, db):
    """웹 프로세스에 얹으면 요청 하나가 느려지고, 별도 스케줄러는 배포에 프로세스를 더한다."""
    from datetime import timedelta

    from sqlalchemy.orm import sessionmaker

    from soriham_api import auth
    from soriham_api.models import UserSession
    from soriham_api.tenancy import create_user
    from soriham_api.worker import idle_maintenance

    hit(db, "옛시도", limit=SMALL, now=datetime.now(UTC) - timedelta(hours=5))
    user = create_user(
        db, email="sweep@example.com", password_hash=auth.hash_password("x"), display_name="s"
    )
    dead = auth.create_session(db, user).session
    dead.expires_at = datetime.now(UTC) - auth.SESSION_KEEP_AFTER - timedelta(days=1)
    dead.absolute_expires_at = dead.expires_at
    db.commit()

    idle_maintenance(sessionmaker(bind=engine))

    db.expire_all()
    assert db.scalar(select(func.count(AuthAttempt.id))) == 0
    assert db.scalar(select(func.count(UserSession.id))) == 0
