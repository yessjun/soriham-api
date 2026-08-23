"""콘솔이 이 응답만으로 화면을 그릴 수 있는가.

없으면 콘솔이 추측하거나 항목마다 상세를 다시 부른다. 콘솔을 만들기 전에 맞춰 두는
편이 싸다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from conftest import TEST_PASSWORD, login, make_settings
from soriham_api import auth
from soriham_api.app import create_app
from soriham_api.models import Recording
from soriham_api.tenancy import add_member, create_user


@pytest.fixture
def app(engine, tmp_path):
    return create_app(
        settings=make_settings(upload_dir=tmp_path / "uploads"),
        session_factory=sessionmaker(bind=engine),
    )


@pytest.fixture
def client(app, owner):
    c = TestClient(app)
    login(c, owner.email)
    return c


def add(db, workspace, name="a.wav", *, source="upload", size=1234):
    recording = Recording(
        workspace_id=workspace.id,
        source=source,
        path=f"/tmp/{workspace.slug}/{name}",
        filename=name,
        size_bytes=size,
        partial_hash=f"{workspace.slug}-{name}",
        status="done",
    )
    db.add(recording)
    db.commit()
    return recording


def test_목록에_유입_경로와_크기가_있다(client, db, workspace):
    """삭제 확인 문구가 갈리고, 용량이 찼을 때 무엇을 지울지 골라야 한다."""
    add(db, workspace, "올린것.wav", source="upload", size=5000)
    add(db, workspace, "스캔한것.wav", source="scan", size=7000)

    items = client.get(f"/api/workspaces/{workspace.public_id}/recordings").json()["items"]

    by_name = {i["filename"]: i for i in items}
    assert by_name["올린것.wav"]["source"] == "upload"
    assert by_name["올린것.wav"]["size_bytes"] == 5000
    assert by_name["스캔한것.wav"]["source"] == "scan"


def test_상세에_삭제_권한이_따로_있다(client, app, db, workspace):
    """공유 상태가 채워졌는지로 추론하면 우연의 일치에 기대는 계약이 된다."""
    recording = add(db, workspace)
    viewer = create_user(
        db,
        email="viewer@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="열람자",
        status="active",
    )
    add_member(db, workspace, viewer, "viewer")
    db.commit()

    mine = client.get(f"/api/recordings/{recording.public_id}").json()
    assert mine["can_manage"] is True

    theirs_client = TestClient(app)
    login(theirs_client, viewer.email)
    theirs = theirs_client.get(f"/api/recordings/{recording.public_id}").json()
    assert theirs["can_edit"] is False
    assert theirs["can_manage"] is False


def test_편집_공유는_고칠_수는_있어도_지울_수는_없다(client, app, db, workspace):
    """can_edit와 can_manage가 갈리는 유일한 자리다. 여기를 안 보면 둘이 같아도 통과한다."""
    recording = add(db, workspace)
    friend = create_user(
        db,
        email="editor@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="편집자",
        status="active",
    )
    db.commit()
    client.post(
        f"/api/recordings/{recording.public_id}/shares",
        json={"email": friend.email, "permission": "edit"},
    )

    theirs = TestClient(app)
    login(theirs, friend.email)
    body = theirs.get(f"/api/recordings/{recording.public_id}").json()

    assert body["can_edit"] is True
    assert body["can_manage"] is False
    assert theirs.delete(f"/api/recordings/{recording.public_id}").status_code == 404


def test_능력은_워크스페이스마다_따로_온다(app, db, workspace, other_workspace):
    """모든 활성 사용자가 자기 개인 워크스페이스의 owner라서, 계정 단위로 주면 항상 켜져 있다."""
    user = create_user(
        db,
        email="mixed@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="섞임",
        status="active",
    )
    add_member(db, workspace, user, "owner")
    add_member(db, other_workspace, user, "member")
    db.commit()

    c = TestClient(app)
    body = login(c, user.email)

    caps = {w["slug"]: w["capabilities"] for w in body["workspaces"]}
    assert "invite" in caps[workspace.slug]
    assert "stats" in caps[workspace.slug]
    assert "invite" not in caps[other_workspace.slug]
    assert "upload" in caps[other_workspace.slug]
    # 계정 단위 목록에는 워크스페이스 능력이 섞이지 않는다
    assert body["capabilities"] == []


def test_열람자에게는_아무_능력도_주지_않는다(app, db, workspace):
    user = create_user(
        db,
        email="onlyview@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="열람만",
        status="active",
    )
    add_member(db, workspace, user, "viewer")
    db.commit()

    c = TestClient(app)
    body = login(c, user.email)

    assert body["workspaces"][0]["capabilities"] == []


def test_서비스_관리자는_계정_단위_능력을_받는다(app, db, workspace, owner):
    owner.is_service_admin = True
    db.commit()

    c = TestClient(app)
    body = login(c, owner.email)

    assert set(body["capabilities"]) == {"admin", "create_workspace"}


def test_공유받은_목록에_권한과_공유한_사람이_있다(client, app, db, workspace, owner):
    """없으면 항목마다 상세를 다시 불러야 편집 가능 여부를 안다."""
    recording = add(db, workspace)
    friend = create_user(
        db,
        email="friend@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="친구",
        status="active",
    )
    db.commit()
    client.post(
        f"/api/recordings/{recording.public_id}/shares",
        json={"email": friend.email, "permission": "edit"},
    )

    theirs = TestClient(app)
    login(theirs, friend.email)
    body = theirs.get("/api/shared-with-me").json()

    assert body["total"] == 1
    assert body["items"][0]["permission"] == "edit"
    assert body["items"][0]["shared_by"] == owner.display_name


def test_구성원_목록에_계정_상태가_있다(client, db, workspace):
    """승인 대기나 중지된 구성원이 정상 구성원과 같아 보이면 안 된다."""
    waiting = create_user(
        db,
        email="waiting@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="대기",
        status="pending",
    )
    add_member(db, workspace, waiting, "member")
    db.commit()

    members = client.get(f"/api/workspaces/{workspace.public_id}/members").json()

    by_email = {m["user"]["email"]: m["status"] for m in members}
    assert by_email["waiting@example.com"] == "pending"
    assert by_email["owner@example.com"] == "active"


def test_업로드하면_올린_사람이_남는다(client, db, workspace, owner):
    """팀 워크스페이스에서 누가 올렸는지 그리려면 기록이 있어야 한다."""
    wav = b"RIFF" + b"\x00" * 2048

    resp = client.post(
        f"/api/workspaces/{workspace.public_id}/recordings",
        files={"file": ("20260817_100000.wav", wav, "audio/wav")},
    )

    assert resp.status_code == 201
    recording = db.scalar(select(Recording).where(Recording.filename == "20260817_100000.wav"))
    assert recording.created_by_user_id == owner.id
