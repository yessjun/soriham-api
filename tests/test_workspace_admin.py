"""워크스페이스 관리: 구성원과 초대.

스키마도 서비스 계층도 있었는데 부를 길이 없었다. `/api/auth/me`가 초대 능력을
내주면서 정작 초대를 발급할 자리가 없는 상태였다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from conftest import TEST_PASSWORD, login, make_settings
from soriham_api import auth
from soriham_api.app import create_app
from soriham_api.models import Invite, Workspace, WorkspaceMember
from soriham_api.tenancy import add_member, create_user


@pytest.fixture
def app(engine):
    return create_app(settings=make_settings(), session_factory=sessionmaker(bind=engine))


@pytest.fixture
def client(app, owner):
    c = TestClient(app)
    login(c, owner.email)
    return c


@pytest.fixture
def friend(db):
    """아직 어느 워크스페이스에도 없는 사람."""
    user = create_user(
        db,
        email="friend@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="친구",
        status="active",
    )
    db.commit()
    return user


def as_user(app, user):
    c = TestClient(app)
    login(c, user.email)
    return c


# --- 구성원 -------------------------------------------------------------------


def test_구성원_목록에_소유자가_보인다(client, workspace, owner):
    body = client.get(f"/api/workspaces/{workspace.public_id}/members").json()

    assert [(m["user"]["email"], m["role"]) for m in body] == [(owner.email, "owner")]


def test_역할을_바꾼다(client, db, workspace, friend):
    add_member(db, workspace, friend, "viewer")
    db.commit()

    resp = client.put(
        f"/api/workspaces/{workspace.public_id}/members/{friend.public_id}",
        json={"role": "member"},
    )

    assert resp.status_code == 200
    assert resp.json()["role"] == "member"


def test_소유자로는_바꿀_수_없다(client, db, workspace, friend):
    """소유권 이전은 범위 밖이다. 허용하면 부분 유니크가 500으로 거절한다."""
    add_member(db, workspace, friend, "member")
    db.commit()

    resp = client.put(
        f"/api/workspaces/{workspace.public_id}/members/{friend.public_id}",
        json={"role": "owner"},
    )

    assert resp.status_code == 422


def test_소유자는_뺄_수_없다(client, workspace, owner):
    resp = client.delete(f"/api/workspaces/{workspace.public_id}/members/{owner.public_id}")

    assert resp.status_code == 422


def test_구성원을_빼면_녹음은_남는다(client, db, workspace, friend):
    """녹음은 워크스페이스 것이다. 사람이 나간다고 사라지면 안 된다."""
    from soriham_api.models import Recording

    add_member(db, workspace, friend, "member")
    db.add(
        Recording(
            workspace_id=workspace.id,
            source="upload",
            path="/tmp/ws/a.wav",
            filename="a.wav",
            size_bytes=10,
            partial_hash="ws-a",
            status="done",
        )
    )
    db.commit()

    assert (
        client.delete(
            f"/api/workspaces/{workspace.public_id}/members/{friend.public_id}"
        ).status_code
        == 204
    )

    assert db.scalar(select(Recording.id)) is not None
    assert db.scalar(select(WorkspaceMember.id).where(WorkspaceMember.user_id == friend.id)) is None


def test_뺀_사람의_기본_워크스페이스를_고쳐_준다(client, db, workspace, other_workspace, friend):
    """기본 워크스페이스가 없어진 사람이 로그인하면 콘솔이 어디로 갈지 모른다."""
    add_member(db, workspace, friend, "member")
    add_member(db, other_workspace, friend, "member")
    friend.default_workspace_id = workspace.id
    db.commit()

    client.delete(f"/api/workspaces/{workspace.public_id}/members/{friend.public_id}")

    db.refresh(friend)
    assert friend.default_workspace_id == other_workspace.id


def test_관리자가_아니면_구성원을_바꿀_수_없다(app, db, workspace, friend):
    add_member(db, workspace, friend, "member")
    db.commit()
    theirs = as_user(app, friend)

    assert theirs.get(f"/api/workspaces/{workspace.public_id}/members").status_code == 200
    assert (
        theirs.delete(
            f"/api/workspaces/{workspace.public_id}/members/{friend.public_id}"
        ).status_code
        == 403
    )


# --- 초대 ---------------------------------------------------------------------


def test_초대를_발급하고_받으면_구성원이_된다(client, app, db, workspace, friend):
    issued = client.post(f"/api/workspaces/{workspace.public_id}/invites", json={}).json()

    theirs = as_user(app, friend)
    preview = theirs.get(f"/api/invites/{issued['token']}").json()
    assert preview["workspace_name"] == workspace.name
    assert preview["role"] == "member"

    joined = theirs.post(f"/api/invites/{issued['token']}/accept")
    assert joined.status_code == 200
    assert joined.json()["slug"] == workspace.slug
    assert theirs.get(f"/api/workspaces/{workspace.public_id}/recordings").status_code == 200


def test_원문_토큰은_발급_응답에만_실린다(client, db, workspace):
    token = client.post(f"/api/workspaces/{workspace.public_id}/invites", json={}).json()["token"]

    listed = client.get(f"/api/workspaces/{workspace.public_id}/invites").json()
    assert "token" not in listed[0]
    assert db.scalars(select(Invite.token_hash)).one() == auth.token_hash(token)


def test_철회한_초대는_없는_것과_같다(client, app, db, workspace, friend):
    issued = client.post(f"/api/workspaces/{workspace.public_id}/invites", json={}).json()
    assert (
        client.delete(f"/api/workspaces/{workspace.public_id}/invites/{issued['id']}").status_code
        == 204
    )

    theirs = as_user(app, friend)
    gone = theirs.get(f"/api/invites/{issued['token']}")
    missing = theirs.get("/api/invites/없는토큰")
    assert gone.status_code == missing.status_code == 404
    assert gone.json()["detail"] == missing.json()["detail"]
    assert theirs.post(f"/api/invites/{issued['token']}/accept").status_code == 404


def test_다_쓴_초대는_목록에서_빠진다(client, app, db, workspace, friend):
    issued = client.post(f"/api/workspaces/{workspace.public_id}/invites", json={}).json()
    as_user(app, friend).post(f"/api/invites/{issued['token']}/accept")

    assert client.get(f"/api/workspaces/{workspace.public_id}/invites").json() == []


def test_이메일을_지정한_초대는_그_사람만_받는다(client, app, db, workspace, friend):
    issued = client.post(
        f"/api/workspaces/{workspace.public_id}/invites",
        json={"email": "someone@example.com"},
    ).json()

    theirs = as_user(app, friend)
    assert theirs.post(f"/api/invites/{issued['token']}/accept").status_code == 404


def test_소유자로는_초대할_수_없다(client, workspace):
    resp = client.post(f"/api/workspaces/{workspace.public_id}/invites", json={"role": "owner"})
    assert resp.status_code == 422


def test_관리자가_아니면_초대를_발급할_수_없다(app, db, workspace, friend):
    add_member(db, workspace, friend, "member")
    db.commit()
    theirs = as_user(app, friend)

    assert theirs.post(f"/api/workspaces/{workspace.public_id}/invites", json={}).status_code == 403
    assert theirs.get(f"/api/workspaces/{workspace.public_id}/invites").status_code == 403


def test_남의_워크스페이스_초대는_철회할_수_없다(client, app, db, other_workspace, friend):
    """워크스페이스로 범위를 좁혀 찾지 않으면 초대 id 하나로 아무 초대나 지워진다."""
    from soriham_api.tenancy import create_invite

    add_member(db, other_workspace, friend, "owner")
    theirs_invite = create_invite(db, workspace=other_workspace).invite
    db.commit()
    ws = db.scalar(select(Workspace).where(Workspace.slug == "test-ws"))

    resp = client.delete(f"/api/workspaces/{ws.public_id}/invites/{theirs_invite.public_id}")

    assert resp.status_code == 404
    db.refresh(theirs_invite)
    assert theirs_invite.revoked_at is None


# --- 워크스페이스 만들기 ---------------------------------------------------------


def test_서비스_관리자만_워크스페이스를_만든다(app, db, workspace, owner, friend):
    """한도가 워크스페이스에 붙으므로 누구나 만들면 한도가 무의미해진다."""
    theirs = as_user(app, friend)
    assert theirs.post("/api/workspaces", json={"name": "팀", "slug": "team"}).status_code == 404

    owner.is_service_admin = True
    db.commit()
    mine = as_user(app, owner)
    resp = mine.post("/api/workspaces", json={"name": "팀", "slug": "team"})

    assert resp.status_code == 201
    assert resp.json()["role"] == "owner"


def test_새_워크스페이스도_기본_한도를_받는다(app, db, owner):
    owner.is_service_admin = True
    db.commit()
    mine = as_user(app, owner)

    mine.post("/api/workspaces", json={"name": "팀", "slug": "team"})

    made = db.scalar(select(Workspace).where(Workspace.slug == "team"))
    assert made.quota_minutes == 600
    assert made.quota_bytes == 20 * 1024 * 1024 * 1024


def test_같은_슬러그는_거절한다(app, db, workspace, owner):
    owner.is_service_admin = True
    db.commit()
    mine = as_user(app, owner)

    resp = mine.post("/api/workspaces", json={"name": "겹침", "slug": workspace.slug})

    assert resp.status_code == 409


def test_이상한_슬러그는_거절한다(app, db, owner):
    owner.is_service_admin = True
    db.commit()
    mine = as_user(app, owner)

    assert (
        mine.post("/api/workspaces", json={"name": "팀", "slug": "대문자 안됨"}).status_code == 422
    )


def test_로그인_없이는_아무것도_못_한다(app, workspace):
    anon = TestClient(app)

    assert anon.get(f"/api/workspaces/{workspace.public_id}/members").status_code == 401
    assert anon.post(f"/api/workspaces/{workspace.public_id}/invites", json={}).status_code == 401
    assert anon.get("/api/invites/아무토큰").status_code == 401
    assert anon.post("/api/workspaces", json={"name": "x", "slug": "x"}).status_code == 401


def test_남의_워크스페이스는_없는_것처럼_답한다(app, other_workspace, owner):
    mine = as_user(app, owner)

    assert mine.get(f"/api/workspaces/{other_workspace.public_id}/members").status_code == 404
    assert (
        mine.post(f"/api/workspaces/{other_workspace.public_id}/invites", json={}).status_code
        == 404
    )


# --- 검토가 잡은 것 --------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"role": "banana"},
        {"role": "Owner"},  # 대문자라 소유자 가드를 지나친다
        {"expires_in_days": 10**12},
        {"expires_in_days": 0},
        {"max_uses": 0},
    ],
)
def test_이상한_초대_입력은_422로_거절한다(client, workspace, body):
    """검사 제약까지 내려가면 사용자에게 500으로 보인다.

    검증을 라우트가 아니라 서비스 계층에 둔다 — 라우트에만 두면 CLI 경로가 같은 값을
    그대로 DB까지 내려보낸다.
    """
    resp = client.post(f"/api/workspaces/{workspace.public_id}/invites", json=body)

    assert resp.status_code == 422, resp.text


def test_이메일을_지정한_초대는_미리보기도_그_사람만_본다(client, app, workspace, friend):
    """수락은 막히는데 이름은 새는 상태였다. 지정 초대의 요지가 미리보기에서 무너진다."""
    issued = client.post(
        f"/api/workspaces/{workspace.public_id}/invites",
        json={"email": "someone-else@example.com"},
    ).json()

    theirs = as_user(app, friend)
    preview = theirs.get(f"/api/invites/{issued['token']}")

    assert preview.status_code == 404
    assert "테스트" not in preview.text
    assert theirs.post(f"/api/invites/{issued['token']}/accept").status_code == 404


def test_만료된_초대는_미리보기도_수락도_안_된다(client, app, db, workspace, friend):
    """만료 판정이 두 경로에 각각 있어 한쪽만 깨져도 아무 테스트가 울지 않았다."""
    from datetime import UTC, datetime, timedelta

    issued = client.post(f"/api/workspaces/{workspace.public_id}/invites", json={}).json()
    invite = db.scalars(select(Invite)).one()
    invite.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    theirs = as_user(app, friend)
    assert theirs.get(f"/api/invites/{issued['token']}").status_code == 404
    assert theirs.post(f"/api/invites/{issued['token']}/accept").status_code == 404


def test_뺀_사람_앞으로_걸린_초대는_함께_철회된다(client, app, db, workspace, friend):
    """남겨 두면 방금 뺀 사람이 같은 토큰으로 다시 들어온다."""
    issued = client.post(
        f"/api/workspaces/{workspace.public_id}/invites",
        json={"email": friend.email, "max_uses": 5},
    ).json()
    theirs = as_user(app, friend)
    assert theirs.post(f"/api/invites/{issued['token']}/accept").status_code == 200

    client.delete(f"/api/workspaces/{workspace.public_id}/members/{friend.public_id}")

    assert theirs.post(f"/api/invites/{issued['token']}/accept").status_code == 404


def test_누구에게나_연_초대는_제외로_닫히지_않는다(client, app, db, workspace, friend):
    """이메일을 지정하지 않은 초대는 특정인이 아니라 열어 둔 자리다. 닫으려면 철회한다."""
    issued = client.post(
        f"/api/workspaces/{workspace.public_id}/invites", json={"max_uses": 5}
    ).json()
    theirs = as_user(app, friend)
    theirs.post(f"/api/invites/{issued['token']}/accept")

    client.delete(f"/api/workspaces/{workspace.public_id}/members/{friend.public_id}")

    assert theirs.post(f"/api/invites/{issued['token']}/accept").status_code == 200
