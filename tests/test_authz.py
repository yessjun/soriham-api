"""인가가 모든 라우트에 실제로 걸려 있는지.

라우트를 하나씩 손으로 확인하지 않는다. 목록을 파라미터라이즈해 두면 새 라우트가
늘 때 같이 넣어야 한다는 것이 눈에 띈다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from conftest import TEST_PASSWORD, login, make_settings
from soriham_api import auth
from soriham_api.app import create_app
from soriham_api.ingest import scan
from soriham_api.models import Recording, Workspace
from soriham_api.tenancy import add_member, create_user
from soriham_api.worker import process_one
from test_worker import FakeRunnerClient


@pytest.fixture
def app_client(engine, tmp_path: Path):
    app = create_app(
        settings=make_settings(audio_dirs=(tmp_path / "rec",)),
        session_factory=sessionmaker(bind=engine),
    )
    return TestClient(app)


@pytest.fixture
def mine(db, tmp_path: Path, workspace, owner) -> Recording:
    path = tmp_path / "rec" / "20260817_100000.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF" + b"\x00" * 64)
    scan(db, (tmp_path / "rec",), workspace_id=workspace.id)
    process_one(db, FakeRunnerClient())
    return db.scalars(select(Recording)).one()


@pytest.fixture
def stranger(db, other_workspace):
    """다른 워크스페이스에만 속한 사람. 남의 녹음이 보이면 안 된다."""
    user = create_user(
        db,
        email="stranger@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="남",
        status="active",
    )
    add_member(db, other_workspace, user, "owner")
    user.default_workspace_id = other_workspace.id
    db.commit()
    return user


def recording_routes(rec: Recording) -> list[tuple[str, str, dict]]:
    pid = rec.public_id
    return [
        ("GET", f"/api/recordings/{pid}", {}),
        ("GET", f"/api/recordings/{pid}/audio", {}),
        ("PATCH", f"/api/recordings/{pid}", {"json": {"title": "바꿈"}}),
        ("PUT", f"/api/recordings/{pid}/speakers/SPEAKER_00", {"json": {"name": "이름"}}),
        ("POST", f"/api/recordings/{pid}/tags", {"json": {"name": "태그"}}),
        (
            "DELETE",
            f"/api/recordings/{pid}/tags/00000000-0000-0000-0000-000000000001",
            {},
        ),
        ("DELETE", f"/api/recordings/{pid}", {}),
        ("GET", f"/api/recordings/{pid}/shares", {}),
        ("POST", f"/api/recordings/{pid}/shares", {"json": {"email": "x@example.com"}}),
        (
            "DELETE",
            f"/api/recordings/{pid}/shares/00000000-0000-0000-0000-000000000001",
            {},
        ),
        ("POST", f"/api/recordings/{pid}/links", {"json": {}}),
        (
            "DELETE",
            f"/api/recordings/{pid}/links/00000000-0000-0000-0000-000000000001",
            {},
        ),
    ]


def workspace_routes(workspace) -> list[tuple[str, str, dict]]:
    base = f"/api/workspaces/{workspace.public_id}"
    return [
        ("GET", f"{base}/recordings", {}),
        ("GET", f"{base}/tags", {}),
        ("GET", f"{base}/search?q=안녕", {}),
        ("GET", f"{base}/stats", {}),
        ("GET", f"{base}/members", {}),
        ("GET", f"{base}/invites", {}),
        ("POST", f"{base}/invites", {"json": {}}),
        (
            "PUT",
            f"{base}/members/00000000-0000-0000-0000-000000000001",
            {"json": {"role": "member"}},
        ),
        ("DELETE", f"{base}/members/00000000-0000-0000-0000-000000000001", {}),
        ("DELETE", f"{base}/invites/00000000-0000-0000-0000-000000000001", {}),
    ]


def test_로그인_없이는_어떤_라우트도_열리지_않는다(app_client, mine, workspace):
    """익명에는 존재를 감추기 전에 401을 준다.

    어떤 id를 넣어도 같은 답이라 탐침이 되지 않고, 세션이 만료된 사람이 로그인
    화면으로 돌아갈 수 있다 — 404를 주면 콘솔은 없는 녹음이라고 표시한다.
    """
    for method, url, kwargs in recording_routes(mine) + workspace_routes(workspace):
        resp = app_client.request(method, url, **kwargs)
        assert resp.status_code == 401, f"{method} {url} → {resp.status_code}"


def test_익명에게는_있는_녹음과_없는_녹음이_같은_답이다(app_client, mine):
    real = app_client.get(f"/api/recordings/{mine.public_id}")
    fake = app_client.get("/api/recordings/00000000-0000-0000-0000-000000000000")
    assert real.status_code == fake.status_code == 401
    assert real.json()["detail"] == fake.json()["detail"]


def test_남의_녹음은_없는_것처럼_답한다(app_client, mine, stranger):
    """403이면 그 녹음이 있다고 알려주는 셈이라 public_id 공간이 멤버십 탐침이 된다."""
    login(app_client, stranger.email)
    for method, url, kwargs in recording_routes(mine):
        resp = app_client.request(method, url, **kwargs)
        assert resp.status_code == 404, f"{method} {url} → {resp.status_code}"


def test_남의_워크스페이스도_없는_것처럼_답한다(app_client, workspace, stranger):
    login(app_client, stranger.email)
    for method, url, kwargs in workspace_routes(workspace):
        resp = app_client.request(method, url, **kwargs)
        assert resp.status_code == 404, f"{method} {url} → {resp.status_code}"


def test_남의_워크스페이스에는_올릴_수_없다(app_client, engine, workspace, stranger, tmp_path):
    login(app_client, stranger.email)
    resp = app_client.post(
        f"/api/workspaces/{workspace.public_id}/recordings",
        files={"file": ("a.wav", b"RIFF" + b"\x00" * 64, "audio/wav")},
    )
    assert resp.status_code == 404


def test_검색은_남의_녹음을_찾아주지_않는다(app_client, db, mine, other_workspace, stranger):
    """세그먼트 분기와 파일명 분기 둘 다 범위를 받아야 한다."""
    login(app_client, stranger.email)
    base = f"/api/workspaces/{other_workspace.public_id}/search"
    assert app_client.get(base, params={"q": "안녕"}).json()["hits"] == []
    assert app_client.get(base, params={"q": "20260817"}).json()["hits"] == []


def test_CSRF_헤더가_없으면_고칠_수_없다(app_client, mine, owner):
    """쿠키는 브라우저가 자동으로 싣는다. 헤더는 우리 화면만 붙일 수 있다."""
    login(app_client, owner.email)
    del app_client.headers["x-csrf-token"]
    resp = app_client.patch(f"/api/recordings/{mine.public_id}", json={"title": "바꿈"})
    assert resp.status_code == 403


def test_CSRF_헤더가_없어도_읽기는_된다(app_client, mine, owner):
    """오디오 태그는 헤더를 실을 수 없다. GET을 면제해야 재생이 된다."""
    login(app_client, owner.email)
    del app_client.headers["x-csrf-token"]
    assert app_client.get(f"/api/recordings/{mine.public_id}").status_code == 200
    assert app_client.get(f"/api/recordings/{mine.public_id}/audio").status_code == 200


def test_틀린_CSRF_토큰도_거절한다(app_client, mine, owner):
    login(app_client, owner.email)
    app_client.headers["x-csrf-token"] = auth.new_token()
    resp = app_client.patch(f"/api/recordings/{mine.public_id}", json={"title": "바꿈"})
    assert resp.status_code == 403


def test_승인_대기_중이면_로그인은_되고_워크스페이스만_막힌다(app_client, db, workspace):
    """인증과 인가를 섞지 않는다. 콘솔이 왜 못 쓰는지를 그려야 한다."""
    pending = create_user(
        db,
        email="pending@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="대기",
        status="pending",
    )
    add_member(db, workspace, pending, "member")
    db.commit()

    login(app_client, pending.email)
    me = app_client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["status"] == "pending"
    assert me.json()["capabilities"] == []

    blocked = app_client.get(f"/api/workspaces/{workspace.public_id}/recordings")
    assert blocked.status_code == 403


def test_승인_대기_중이면_남의_녹음도_못_본다(app_client, db, workspace, mine):
    pending = create_user(
        db,
        email="pending@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="대기",
        status="pending",
    )
    add_member(db, workspace, pending, "member")
    db.commit()

    login(app_client, pending.email)
    assert app_client.get(f"/api/recordings/{mine.public_id}").status_code == 403


def test_거절된_계정은_로그인부터_막힌다(app_client, db):
    rejected = create_user(
        db,
        email="rejected@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="거절",
        status="rejected",
    )
    db.commit()
    resp = app_client.post(
        "/api/auth/login", json={"email": rejected.email, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 403


def test_없는_계정과_틀린_비밀번호가_같은_답을_준다(app_client, db, owner):
    """다르면 어느 이메일이 가입돼 있는지 알아낼 수 있다."""
    wrong = app_client.post(
        "/api/auth/login", json={"email": owner.email, "password": "틀린 암구호"}
    )
    missing = app_client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "아무 말"}
    )
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["detail"] == missing.json()["detail"]


def test_로그아웃하면_세션이_죽는다(app_client, owner, mine):
    login(app_client, owner.email)
    assert app_client.post("/api/auth/logout").status_code == 204
    assert app_client.get(f"/api/recordings/{mine.public_id}").status_code == 401


def test_통계는_워크스페이스_관리자만_본다(app_client, db, workspace):
    """처리 파이프라인 뷰다. 최근 에러가 녹음 제목을 나열한다."""
    viewer = create_user(
        db,
        email="viewer@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="열람자",
        status="active",
    )
    add_member(db, workspace, viewer, "viewer")
    db.commit()

    login(app_client, viewer.email)
    assert app_client.get(f"/api/workspaces/{workspace.public_id}/stats").status_code == 403
    assert app_client.get(f"/api/workspaces/{workspace.public_id}/recordings").status_code == 200


def test_열람자는_고칠_수_없다(app_client, db, workspace, mine):
    viewer = create_user(
        db,
        email="viewer@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="열람자",
        status="active",
    )
    add_member(db, workspace, viewer, "viewer")
    db.commit()

    login(app_client, viewer.email)
    detail = app_client.get(f"/api/recordings/{mine.public_id}")
    assert detail.status_code == 200
    assert detail.json()["can_edit"] is False
    assert (
        app_client.patch(f"/api/recordings/{mine.public_id}", json={"title": "바꿈"}).status_code
        == 404
    )


def test_소유자는_고칠_수_있다고_응답에_적힌다(app_client, owner, mine):
    """콘솔이 역할 산술을 다시 하면 두 규칙이 반드시 어긋난다."""
    login(app_client, owner.email)
    assert app_client.get(f"/api/recordings/{mine.public_id}").json()["can_edit"] is True


def test_운영_모드에서는_스키마를_열지_않는다(engine):
    """라우트 목록 자체가 정찰 재료다."""
    closed = TestClient(
        create_app(settings=make_settings(), session_factory=sessionmaker(bind=engine))
    )
    assert closed.get("/openapi.json").status_code == 404
    opened = TestClient(
        create_app(
            settings=make_settings(expose_docs=True), session_factory=sessionmaker(bind=engine)
        )
    )
    assert opened.get("/openapi.json").status_code == 200


def test_통계는_남의_워크스페이스_녹음을_세지_않는다(
    app_client, db, mine, other_workspace, stranger
):
    """라우트를 관리자로 막아도 질의 안쪽 범위를 안 걸면 그대로 샌다.

    최근 에러가 녹음 제목을 나열하므로, 범위가 없으면 남의 회의 제목이 그대로 보인다.
    """
    mine.status = "error"
    mine.error = "일부러 낸 실패"
    db.commit()

    login(app_client, stranger.email)
    body = app_client.get(f"/api/workspaces/{other_workspace.public_id}/stats").json()

    assert body["recent_errors"] == []
    assert sum(row["count"] for row in body["by_status"]) == 0


def test_목록은_자기_워크스페이스만_보여준다(app_client, db, mine, other_workspace, owner):
    """가장 중요한 필터인데 지워도 아무 테스트가 울지 않았다."""
    theirs = Recording(
        workspace_id=other_workspace.id,
        source="upload",
        path="/tmp/theirs/b.wav",
        filename="theirs.wav",
        size_bytes=10,
        partial_hash="theirs",
        status="done",
    )
    db.add(theirs)
    db.commit()

    login(app_client, owner.email)
    ws = db.get(Workspace, mine.workspace_id)
    body = app_client.get(f"/api/workspaces/{ws.public_id}/recordings").json()

    assert body["total"] == 1
    names = {item["filename"] for item in body["items"]}
    assert "theirs.wav" not in names


def test_검색도_자기_워크스페이스만_찾는다(app_client, db, mine, other_workspace, owner):
    theirs = Recording(
        workspace_id=other_workspace.id,
        source="upload",
        path="/tmp/theirs/c.wav",
        filename="20260817_findme.wav",
        size_bytes=10,
        partial_hash="theirs-c",
        status="done",
        title="남의 회의",
    )
    db.add(theirs)
    db.commit()
    ws = db.get(Workspace, mine.workspace_id)

    login(app_client, owner.email)
    hits = app_client.get(
        f"/api/workspaces/{ws.public_id}/search", params={"q": "20260817"}
    ).json()["hits"]
    assert all(h["recording"]["filename"] != "20260817_findme.wav" for h in hits)


def test_승인_대기_중에는_있는_것과_없는_것이_같은_답이다(app_client, db, workspace, mine):
    """계정 상태 검사가 조회 뒤에 있으면 403과 404가 갈려 존재가 드러난다."""
    pending = create_user(
        db,
        email="pending@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="대기",
        status="pending",
    )
    add_member(db, workspace, pending, "member")
    db.commit()

    login(app_client, pending.email)
    real = app_client.get(f"/api/recordings/{mine.public_id}")
    fake = app_client.get("/api/recordings/00000000-0000-0000-0000-000000000000")
    assert real.status_code == fake.status_code == 403
    assert real.json()["detail"] == fake.json()["detail"]


def test_행이_위조돼도_남의_파일은_내보내지_않는다(app_client, db, mine, owner, tmp_path):
    """권한 검사를 통과한 뒤에도 경로가 허용된 뿌리 밖이면 내보내지 않는다.

    행이 손상되거나 옛 배치가 남아 있어도 남의 오디오가 나가지 않게 하는 마지막
    검사다. 권한만 보고 경로를 그대로 여는 것이 이 전환 전의 동작이었다.
    """
    secret = tmp_path / "elsewhere" / "secret.wav"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_bytes(b"RIFF" + b"\x00" * 64)
    mine.path = str(secret)
    db.commit()

    login(app_client, owner.email)
    forged = app_client.get(f"/api/recordings/{mine.public_id}/audio")
    assert forged.status_code == 404

    # 뿌리 밖인 것과 파일이 없는 것은 같은 답이어야 한다. 다르면 어떤 경로가
    # 존재하는지 알려주는 셈이다
    mine.path = str(tmp_path / "rec" / "사라진.wav")
    db.commit()
    missing = app_client.get(f"/api/recordings/{mine.public_id}/audio")
    assert missing.status_code == 404
    assert forged.json()["detail"] == missing.json()["detail"]


def test_남은_시간은_자기_대기분만_센다(app_client, db, mine, other_workspace, owner):
    """전역으로 세면 남의 작업량이 그대로 드러난다."""
    from soriham_api.models import JobLog, Workspace

    ws = db.get(Workspace, mine.workspace_id)
    mine.status = "pending"
    mine.duration_sec = 600.0
    theirs = Recording(
        workspace_id=other_workspace.id,
        source="upload",
        path="/tmp/theirs/big.wav",
        filename="big.wav",
        size_bytes=10,
        partial_hash="theirs-big",
        status="pending",
        duration_sec=36000.0,
    )
    db.add(theirs)
    db.add(
        JobLog(
            workspace_id=ws.id,
            stage="transcribe",
            status="done",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            audio_sec=100.0,
            elapsed_sec=10.0,
        )
    )
    db.commit()

    login(app_client, owner.email)
    body = app_client.get(f"/api/workspaces/{ws.public_id}/stats").json()

    assert body["speed_ratio"] == pytest.approx(0.1)
    # 자기 것 600초만 센다. 남의 10시간이 섞이면 3660이 나온다
    assert body["eta_sec"] == pytest.approx(60.0)


def test_처리_배속은_남의_실측으로도_낸다(app_client, db, mine, other_workspace, owner):
    """배속은 이 기계의 특성이다. 나누면 새 워크스페이스가 남은 시간을 아예 못 낸다."""
    from soriham_api.models import JobLog, Workspace

    ws = db.get(Workspace, mine.workspace_id)
    mine.status = "pending"
    mine.duration_sec = 600.0
    db.add(
        JobLog(
            workspace_id=other_workspace.id,
            stage="transcribe",
            status="done",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            audio_sec=100.0,
            elapsed_sec=20.0,
        )
    )
    db.commit()

    login(app_client, owner.email)
    body = app_client.get(f"/api/workspaces/{ws.public_id}/stats").json()

    assert body["speed_ratio"] == pytest.approx(0.2)
    assert body["eta_sec"] == pytest.approx(120.0)


def test_계정_상태마다_다른_문구를_준다(app_client, db, workspace, mine):
    """같은 판단이 표면마다 다른 문구를 내면 사용자가 상태를 오해한다."""
    blocked = create_user(
        db,
        email="blocked@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="중지",
        status="active",
    )
    add_member(db, workspace, blocked, "member")
    db.commit()
    login(app_client, blocked.email)
    blocked.status = "disabled"
    db.commit()

    detail = app_client.get(f"/api/recordings/{mine.public_id}")
    listing = app_client.get(f"/api/workspaces/{workspace.public_id}/recordings")

    assert detail.status_code == listing.status_code == 403
    assert detail.json()["detail"] == "사용이 중지된 계정입니다"
    assert listing.json()["detail"] == detail.json()["detail"]
