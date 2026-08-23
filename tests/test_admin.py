"""계정 상태 관리: 승인, 거절, 중지, 재개.

이 표면에는 테스트가 없었다. 라우트 이름을 통째로 바꿔도 아무것도 깨지지 않았다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from conftest import TEST_PASSWORD, login, make_settings
from soriham_api import auth
from soriham_api.app import create_app
from soriham_api.models import User, UserSession
from soriham_api.tenancy import signup


@pytest.fixture
def app(engine):
    return create_app(settings=make_settings(), session_factory=sessionmaker(bind=engine))


@pytest.fixture
def admin(db, owner):
    owner.is_service_admin = True
    db.commit()
    return owner


@pytest.fixture
def client(app, admin):
    c = TestClient(app)
    login(c, admin.email)
    return c


@pytest.fixture
def applicant(db):
    result = signup(db, email="new@example.com", password=TEST_PASSWORD, display_name="신청자")
    db.commit()
    return result.user


def set_status(client, user, status):
    return client.put(f"/api/admin/users/{user.public_id}/status", json={"status": status})


def test_대기_목록에_신청자가_보인다(client, applicant):
    rows = client.get("/api/admin/users").json()

    assert [r["email"] for r in rows] == [applicant.email]
    assert rows[0]["status"] == "pending"


def test_거절한_계정도_찾을_수_있다(client, db, applicant):
    """대기만 볼 수 있으면 거절된 계정을 되돌릴 길이 없다. 이메일이 남아 재가입도 막힌다."""
    set_status(client, applicant, "rejected")

    assert client.get("/api/admin/users").json() == []
    rejected = client.get("/api/admin/users", params={"status": "rejected"}).json()
    assert [r["email"] for r in rejected] == [applicant.email]


def test_거절했다가_되돌릴_수_있다(client, db, applicant):
    set_status(client, applicant, "rejected")

    resp = set_status(client, applicant, "active")

    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    # 쓸 워크스페이스가 다시 생긴다 — 거절이 지웠기 때문이다
    assert resp.json()["workspaces"] != []


def test_계정을_중지할_수_있다(client, app, db, applicant):
    """중지로 가는 전이가 어디에도 없어서 상태값이 죽어 있었다."""
    set_status(client, applicant, "active")

    resp = set_status(client, applicant, "disabled")

    assert resp.status_code == 200
    db.refresh(applicant)
    assert applicant.status == "disabled"
    blocked = TestClient(app).post(
        "/api/auth/login", json={"email": applicant.email, "password": TEST_PASSWORD}
    )
    assert blocked.status_code == 403


def test_중지하면_살아있는_세션이_끊긴다(client, app, db, applicant):
    """안 끊으면 중지된 사람의 브라우저가 최대 90일 동안 살아 있다."""
    set_status(client, applicant, "active")
    theirs = TestClient(app)
    login(theirs, applicant.email)
    assert theirs.get("/api/auth/me").status_code == 200

    set_status(client, applicant, "disabled")

    assert theirs.get("/api/auth/me").status_code == 401
    live = db.scalars(
        select(UserSession).where(
            UserSession.user_id == applicant.id, UserSession.revoked_at.is_(None)
        )
    ).all()
    assert live == []


def test_중지했다_재개할_수_있다(client, app, db, applicant):
    set_status(client, applicant, "active")
    set_status(client, applicant, "disabled")

    set_status(client, applicant, "active")

    again = TestClient(app)
    assert (
        again.post(
            "/api/auth/login", json={"email": applicant.email, "password": TEST_PASSWORD}
        ).status_code
        == 200
    )


def test_자기_계정의_상태는_못_바꾼다(client, admin):
    assert set_status(client, admin, "disabled").status_code == 422


def test_없는_상태로는_못_옮긴다(client, applicant):
    assert set_status(client, applicant, "pending").status_code == 422
    assert set_status(client, applicant, "banana").status_code == 422


def test_이상한_상태로_거르면_거절한다(client):
    assert client.get("/api/admin/users", params={"status": "banana"}).status_code == 422


def test_서비스_관리자가_아니면_없는_것처럼_답한다(app, db, applicant):
    """관리 표면이 있다는 것 자체를 알려주지 않는다."""
    plain = create_plain_user(db)
    theirs = TestClient(app)
    login(theirs, plain.email)

    assert theirs.get("/api/admin/users").status_code == 404
    assert (
        theirs.put(
            f"/api/admin/users/{applicant.public_id}/status", json={"status": "active"}
        ).status_code
        == 404
    )


def test_로그인_없이는_열리지_않는다(app, applicant):
    anon = TestClient(app)

    assert anon.get("/api/admin/users").status_code == 401
    assert (
        anon.put(
            f"/api/admin/users/{applicant.public_id}/status", json={"status": "active"}
        ).status_code
        == 401
    )


def test_CSRF_헤더가_없으면_상태를_못_바꾼다(client, applicant):
    del client.headers["x-csrf-token"]

    assert set_status(client, applicant, "active").status_code == 403


def create_plain_user(db) -> User:
    from soriham_api.tenancy import create_user

    user = create_user(
        db,
        email="plain@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="보통",
        status="active",
    )
    db.commit()
    return user
