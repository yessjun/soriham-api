"""공유: 사람에게 열기, 링크로 열기, 나에게 열린 것 보기.

돈이 걸린 것은 한도이고 프라이버시가 걸린 것은 여기다. 링크 하나가 회의 전문과
참석자 실명을 로그인 없이 내보낸다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from conftest import TEST_PASSWORD, login, make_settings
from soriham_api import auth
from soriham_api.app import create_app
from soriham_api.ingest import scan
from soriham_api.models import Recording, RecordingShare, ShareLink, SpeakerName
from soriham_api.tenancy import add_member, approve, create_user, signup
from soriham_api.worker import process_one
from test_worker import FakeRunnerClient

LINK_PASSWORD = "링크 암구호"


@pytest.fixture
def app(engine, tmp_path: Path):
    return create_app(
        settings=make_settings(audio_dirs=(tmp_path / "rec",)),
        session_factory=sessionmaker(bind=engine),
    )


@pytest.fixture
def client(app, owner):
    c = TestClient(app)
    login(c, owner.email)
    return c


@pytest.fixture
def guest(app):
    """쿠키가 없는 브라우저. 링크 열람자는 이 자리에서 온다."""
    return TestClient(app)


@pytest.fixture
def mine(db, tmp_path: Path, workspace) -> Recording:
    path = tmp_path / "rec" / "20260817_인사평가.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF" + b"\x00" * 4096)
    scan(db, (tmp_path / "rec",), workspace_id=workspace.id)
    process_one(db, FakeRunnerClient())
    rec = db.scalars(select(Recording)).one()
    db.add(SpeakerName(recording_id=rec.id, speaker_key="SPEAKER_00", display_name="김실명"))
    rec.title = "8월 정기 회의"
    rec.summary = "요약입니다"
    rec.error = "지난번 실패 흔적"
    db.commit()
    return rec


@pytest.fixture
def friend(db, other_workspace):
    """다른 워크스페이스 사람. 공유가 없으면 이 녹음에 닿을 수 없다."""
    user = create_user(
        db,
        email="friend@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="친구",
        status="active",
    )
    add_member(db, other_workspace, user, "owner")
    user.default_workspace_id = other_workspace.id
    db.commit()
    return user


def make_link(client, rec, **body):
    resp = client.post(f"/api/recordings/{rec.public_id}/links", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- 링크 ---------------------------------------------------------------------


def test_링크는_로그인_없이_열린다(client, guest, mine):
    token = make_link(client, mine)["token"]

    body = guest.get(f"/api/shared/{token}").json()

    assert body["title"] == "8월 정기 회의"
    assert body["summary"] == "요약입니다"
    assert [s["text"] for s in body["segments"]] == ["안녕하세요", "반갑습니다"]


def test_링크_응답에_파일명과_오류와_엔진_메타가_없다(client, guest, mine):
    """파일명이 곧 내용을 말한다. 오류와 엔진 메타는 바깥 사람이 볼 것이 아니다."""
    token = make_link(client, mine)["token"]

    body = guest.get(f"/api/shared/{token}").json()

    assert "filename" not in body
    assert "error" not in body
    assert "stt_meta" not in body
    assert "id" not in body


def test_링크_오디오는_인증_헤더_없이_206을_준다(client, guest, mine):
    """오디오 태그는 헤더를 실을 수 없다. 토큰이 경로에 있는 이유가 이것이다."""
    token = make_link(client, mine)["token"]

    resp = guest.get(f"/api/shared/{token}/audio", headers={"range": "bytes=0-99"})

    assert resp.status_code == 206
    assert resp.headers["content-range"].startswith("bytes 0-99/")
    assert len(resp.content) == 100


def test_원문_토큰은_발급_응답에만_실린다(client, db, mine):
    token = make_link(client, mine)["token"]

    listed = client.get(f"/api/recordings/{mine.public_id}/shares").json()["links"]
    assert "token" not in listed[0]

    stored = db.scalars(select(ShareLink)).one()
    assert stored.token_hash != token
    assert stored.token_hash == auth.token_hash(token)


def test_철회한_링크는_없는_것과_같다(client, guest, mine):
    issued = make_link(client, mine)
    assert guest.get(f"/api/shared/{issued['token']}").status_code == 200

    assert (
        client.delete(f"/api/recordings/{mine.public_id}/links/{issued['id']}").status_code == 204
    )

    gone = guest.get(f"/api/shared/{issued['token']}")
    missing = guest.get("/api/shared/없는토큰")
    assert gone.status_code == missing.status_code == 404
    assert gone.json()["detail"] == missing.json()["detail"]


def test_만료된_링크는_없는_것과_같다(client, guest, db, mine):
    issued = make_link(client, mine, expires_in_days=1)
    link = db.scalars(select(ShareLink)).one()
    link.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    assert guest.get(f"/api/shared/{issued['token']}").status_code == 404


def test_만료_기간을_비우면_무기한이다(client, db, mine):
    """30일이 기본이지만 남겨 두고 싶은 회의록이 있다."""
    make_link(client, mine, expires_in_days=None)

    assert db.scalars(select(ShareLink)).one().expires_at is None


def test_기본_만료는_30일이다(client, db, mine):
    make_link(client, mine)

    link = db.scalars(select(ShareLink)).one()
    assert 29 < (link.expires_at - datetime.now(UTC)).days + 1 <= 30


def test_말도_안_되는_만료_기간은_거절한다(client, mine):
    resp = client.post(f"/api/recordings/{mine.public_id}/links", json={"expires_in_days": 0})
    assert resp.status_code == 422


# --- 링크 비밀번호 -------------------------------------------------------------


def test_비밀번호_링크는_잠금_해제_전에는_상세도_막힌다(client, guest, mine):
    """오디오만 막으면 요약이 비밀번호 없이 샌다."""
    token = make_link(client, mine, password=LINK_PASSWORD)["token"]

    assert guest.get(f"/api/shared/{token}").status_code == 401
    assert guest.get(f"/api/shared/{token}/audio").status_code == 401

    assert (
        guest.post(f"/api/shared/{token}/unlock", json={"password": LINK_PASSWORD}).status_code
        == 204
    )

    assert guest.get(f"/api/shared/{token}").status_code == 200
    assert guest.get(f"/api/shared/{token}/audio").status_code == 200


def test_틀린_비밀번호로는_열리지_않는다(client, guest, mine):
    token = make_link(client, mine, password=LINK_PASSWORD)["token"]

    wrong = guest.post(f"/api/shared/{token}/unlock", json={"password": "아무 말"})
    assert wrong.status_code == 403
    assert guest.get(f"/api/shared/{token}").status_code == 401


def test_잠금_해제_쿠키는_그_링크_경로에만_붙는다(client, guest, mine):
    """쿠키가 모든 주소에 실리면 링크 하나를 푼 브라우저가 그 사실을 계속 흘린다.

    값 자체도 링크마다 다르지만(해시에서 파생한다) 경로를 좁히는 것이 앞선 방벽이다.
    이 단언이 없으면 경로를 넓혀도 아무 테스트가 울지 않는다.
    """
    token = make_link(client, mine, password=LINK_PASSWORD)["token"]

    resp = guest.post(f"/api/shared/{token}/unlock", json={"password": LINK_PASSWORD})

    assert f"Path=/api/shared/{token}" in resp.headers["set-cookie"]
    assert "HttpOnly" in resp.headers["set-cookie"]


def test_한_링크를_풀어도_다른_링크는_잠겨_있다(client, guest, mine):
    locked = make_link(client, mine, password=LINK_PASSWORD)["token"]
    guest.post(f"/api/shared/{locked}/unlock", json={"password": LINK_PASSWORD})

    another = make_link(client, mine, password=LINK_PASSWORD)["token"]

    assert guest.get(f"/api/shared/{locked}").status_code == 200
    assert guest.get(f"/api/shared/{another}").status_code == 401


def test_비밀번호를_바꾸면_나간_잠금_해제가_죽는다(client, guest, db, mine):
    token = make_link(client, mine, password=LINK_PASSWORD)["token"]
    guest.post(f"/api/shared/{token}/unlock", json={"password": LINK_PASSWORD})
    assert guest.get(f"/api/shared/{token}").status_code == 200

    link = db.scalars(select(ShareLink)).one()
    link.password_hash = auth.hash_password("새 암구호")
    db.commit()

    assert guest.get(f"/api/shared/{token}").status_code == 401


def test_공백만_넣은_비밀번호는_링크를_주지_않는다(client, mine):
    """걸었다고 믿는데 안 걸린 링크가 나가는 것이 가장 나쁘다."""
    resp = client.post(f"/api/recordings/{mine.public_id}/links", json={"password": "   "})

    assert resp.status_code == 422


def test_앞뒤_공백이_있는_비밀번호도_알려준_그대로_통한다(client, guest, mine):
    """발급만 다듬으면 발급자가 알려준 그대로 넣은 열람자가 막힌다."""
    token = make_link(client, mine, password=" 여백 있는 암구호 ")["token"]

    assert (
        guest.post(
            f"/api/shared/{token}/unlock", json={"password": " 여백 있는 암구호 "}
        ).status_code
        == 204
    )


def test_스스로_볼_권한이_있으면_잠금에_걸리지_않는다(client, mine):
    """구성원은 어차피 다른 길로 같은 것을 본다. 자기 링크에 자기가 막히면 이상하다."""
    token = make_link(client, mine, password=LINK_PASSWORD)["token"]

    assert client.get(f"/api/shared/{token}").status_code == 200


# --- 링크의 잠금 ---------------------------------------------------------------


def test_화자_이름_노출을_끄면_실명이_나가지_않는다(client, guest, mine):
    """화자 이름은 손으로 넣은 실명이다. 노출을 정한 것은 요약과 태그였다."""
    token = make_link(client, mine, allow_speaker_names=False)["token"]

    body = guest.get(f"/api/shared/{token}").json()

    assert body["speaker_names"] == {}
    assert "김실명" not in guest.get(f"/api/shared/{token}").text
    # 라벨은 남는다 — 화자 구분이 없으면 회의록이 읽히지 않는다
    assert body["segments"][0]["speaker_key"] == "SPEAKER_00"


def test_화자_이름을_켜면_그대로_나간다(client, guest, mine):
    token = make_link(client, mine, allow_speaker_names=True)["token"]

    assert guest.get(f"/api/shared/{token}").json()["speaker_names"] == {"SPEAKER_00": "김실명"}


def test_오디오를_막은_링크는_재생을_주지_않는다(client, guest, mine):
    token = make_link(client, mine, allow_audio=False)["token"]

    assert guest.get(f"/api/shared/{token}").json()["allow_audio"] is False
    assert guest.get(f"/api/shared/{token}/audio").status_code == 403


def test_잠금은_로그인한_남에게도_걸린다(client, app, db, friend, mine):
    """로그인 여부로 가르면 승인제 서비스에서 잠금이 아무도 못 막는다."""
    token = make_link(client, mine, allow_audio=False, allow_speaker_names=False)["token"]

    other = TestClient(app)
    login(other, friend.email)

    body = other.get(f"/api/shared/{token}").json()
    assert body["speaker_names"] == {}
    assert other.get(f"/api/shared/{token}/audio").status_code == 403


def test_링크를_들고_있어도_워크스페이스에는_닿지_못한다(client, guest, workspace, mine):
    """링크는 그 녹음 하나에만 권한을 준다.

    녹음 단위 라우트는 비로그인을 조회 전에 401로 막으므로 여기서 시험할 것이 없다
    (어떤 id를 넣어도 같은 답이다). 링크가 넓어졌을 때 실제로 드러나는 자리는
    워크스페이스 컬렉션이다. 녹음 범위 자체는 test_permissions가 순수 함수로 지킨다.
    """
    token = make_link(client, mine)["token"]
    assert guest.get(f"/api/shared/{token}").status_code == 200

    base = f"/api/workspaces/{workspace.public_id}"
    assert guest.get(f"{base}/recordings").status_code == 401
    assert guest.get(f"{base}/search", params={"q": "안녕"}).status_code == 401


def test_조회수가_올라간다(client, guest, db, mine):
    token = make_link(client, mine)["token"]
    guest.get(f"/api/shared/{token}")
    guest.get(f"/api/shared/{token}")

    db.expire_all()
    link = db.scalars(select(ShareLink)).one()
    assert link.view_count == 2
    assert link.last_viewed_at is not None


# --- 공개 응답의 헤더 -----------------------------------------------------------


def test_공개_응답은_색인과_캐시를_막는다(client, guest, mine):
    token = make_link(client, mine)["token"]

    resp = guest.get(f"/api/shared/{token}")

    assert resp.headers["cache-control"] == "private, no-store"
    assert "noindex" in resp.headers["x-robots-tag"]


def test_공개_응답은_참조_주소도_보내지_않는다(client, guest, mine):
    """토큰이 주소에 있다. 다만 이 헤더가 실제로 물리는 자리는 콘솔의 열람 화면이다."""
    token = make_link(client, mine)["token"]

    assert guest.get(f"/api/shared/{token}").headers["referrer-policy"] == "no-referrer"
    audio = guest.get(f"/api/shared/{token}/audio")
    assert audio.headers["referrer-policy"] == "no-referrer"


# --- 사용자 지정 공유 -----------------------------------------------------------


def test_열람_공유는_보기만_된다(client, app, friend, mine):
    client.post(
        f"/api/recordings/{mine.public_id}/shares",
        json={"email": friend.email, "permission": "view"},
    )

    theirs = TestClient(app)
    login(theirs, friend.email)
    detail = theirs.get(f"/api/recordings/{mine.public_id}")
    assert detail.status_code == 200
    assert detail.json()["can_edit"] is False
    assert (
        theirs.patch(f"/api/recordings/{mine.public_id}", json={"title": "바꿈"}).status_code == 404
    )


def test_편집_공유는_고칠_수_있고_다시_공유할_수는_없다(client, app, friend, mine):
    """EDIT가 상한이다. 공유받은 사람이 재공유하면 소유자가 통제를 잃는다."""
    client.post(
        f"/api/recordings/{mine.public_id}/shares",
        json={"email": friend.email, "permission": "edit"},
    )

    theirs = TestClient(app)
    login(theirs, friend.email)
    assert theirs.get(f"/api/recordings/{mine.public_id}").json()["can_edit"] is True
    assert (
        theirs.patch(f"/api/recordings/{mine.public_id}", json={"title": "바꿈"}).status_code == 200
    )

    assert (
        theirs.post(
            f"/api/recordings/{mine.public_id}/shares",
            json={"email": "someone@example.com"},
        ).status_code
        == 404
    )
    assert theirs.get(f"/api/recordings/{mine.public_id}/shares").status_code == 404
    assert theirs.delete(f"/api/recordings/{mine.public_id}").status_code == 404


def test_공유는_받는_사람의_목록에_나타난다(client, app, friend, mine):
    """목록과 검색은 워크스페이스만 훑는다. 이 화면이 없으면 도달할 길이 없다."""
    client.post(f"/api/recordings/{mine.public_id}/shares", json={"email": friend.email})

    theirs = TestClient(app)
    login(theirs, friend.email)
    body = theirs.get("/api/shared-with-me").json()

    assert body["total"] == 1
    assert body["items"][0]["id"] == str(mine.public_id)


def test_공유받지_않으면_목록이_비어_있다(app, friend, mine):
    theirs = TestClient(app)
    login(theirs, friend.email)

    assert theirs.get("/api/shared-with-me").json() == {"items": [], "total": 0}


def test_같은_사람에게_다시_공유하면_권한만_바뀐다(client, db, friend, mine):
    client.post(
        f"/api/recordings/{mine.public_id}/shares",
        json={"email": friend.email, "permission": "view"},
    )
    client.post(
        f"/api/recordings/{mine.public_id}/shares",
        json={"email": friend.email, "permission": "edit"},
    )

    share = db.scalars(select(RecordingShare)).one()
    assert share.permission == "edit"


def test_가입하지_않은_이메일로도_미리_공유한다(client, db, mine):
    """미가입이라고 거절하면 가입시키고 다시 공유하라는 절차가 생긴다."""
    resp = client.post(
        f"/api/recordings/{mine.public_id}/shares", json={"email": "새사람@example.com"}
    )

    assert resp.status_code == 201
    assert resp.json()["pending"] is True
    assert db.scalars(select(RecordingShare)).one().user_id is None


def test_예약_공유는_승인_때_그_사람의_목록에_들어온다(client, app, db, mine):
    client.post(f"/api/recordings/{mine.public_id}/shares", json={"email": "late@example.com"})
    result = signup(db, email="late@example.com", password=TEST_PASSWORD, display_name="늦은 사람")
    approve(db, result.user)
    db.commit()

    theirs = TestClient(app)
    login(theirs, "late@example.com")

    assert theirs.get("/api/shared-with-me").json()["total"] == 1


def test_자기_자신에게는_공유할_수_없다(client, owner, mine):
    resp = client.post(f"/api/recordings/{mine.public_id}/shares", json={"email": owner.email})
    assert resp.status_code == 422


def test_이상한_이메일은_받지_않는다(client, mine):
    resp = client.post(f"/api/recordings/{mine.public_id}/shares", json={"email": "그냥 글자"})
    assert resp.status_code == 422


def test_없는_권한_이름은_받지_않는다(client, friend, mine):
    resp = client.post(
        f"/api/recordings/{mine.public_id}/shares",
        json={"email": friend.email, "permission": "manage"},
    )
    assert resp.status_code == 422


def test_공유를_철회하면_다시_닿지_못한다(client, app, friend, mine):
    created = client.post(
        f"/api/recordings/{mine.public_id}/shares", json={"email": friend.email}
    ).json()

    assert (
        client.delete(f"/api/recordings/{mine.public_id}/shares/{created['id']}").status_code == 204
    )

    theirs = TestClient(app)
    login(theirs, friend.email)
    assert theirs.get(f"/api/recordings/{mine.public_id}").status_code == 404


def test_철회는_다른_녹음의_공유를_건드리지_않는다(client, db, workspace, friend, mine):
    """녹음으로 범위를 좁혀 찾지 않으면 남의 공유 id 하나로 아무 공유나 지워진다."""
    other = Recording(
        workspace_id=workspace.id,
        source="upload",
        path="/tmp/other/c.wav",
        filename="c.wav",
        size_bytes=10,
        partial_hash="other-c",
        status="done",
    )
    db.add(other)
    db.commit()
    theirs = client.post(
        f"/api/recordings/{other.public_id}/shares", json={"email": friend.email}
    ).json()

    resp = client.delete(f"/api/recordings/{mine.public_id}/shares/{theirs['id']}")

    assert resp.status_code == 404
    assert db.scalar(select(RecordingShare).where(RecordingShare.public_id == theirs["id"]))


# --- 공유 상태 -----------------------------------------------------------------


def test_공유_상태는_관리할_수_있는_사람에게만_실린다(client, app, db, workspace, friend, mine):
    """열람자에게 "3명과 공유됨"을 보여줄 이유가 없다."""
    client.post(f"/api/recordings/{mine.public_id}/shares", json={"email": friend.email})
    make_link(client, mine)

    state = client.get(f"/api/recordings/{mine.public_id}").json()["share_state"]
    assert state == {"user_count": 1, "link_count": 1}

    viewer = create_user(
        db,
        email="viewer@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="열람자",
        status="active",
    )
    add_member(db, workspace, viewer, "viewer")
    db.commit()
    theirs = TestClient(app)
    login(theirs, viewer.email)

    assert theirs.get(f"/api/recordings/{mine.public_id}").json()["share_state"] is None


def test_철회한_링크는_공유_상태에서_빠진다(client, mine):
    issued = make_link(client, mine)
    client.delete(f"/api/recordings/{mine.public_id}/links/{issued['id']}")

    state = client.get(f"/api/recordings/{mine.public_id}").json()["share_state"]
    assert state == {"user_count": 0, "link_count": 0}


# --- 인가 ----------------------------------------------------------------------


def test_남의_녹음에는_공유를_만들_수_없다(app, friend, mine):
    theirs = TestClient(app)
    login(theirs, friend.email)

    assert theirs.post(f"/api/recordings/{mine.public_id}/links", json={}).status_code == 404


def test_로그인_없이는_공유를_만들_수_없다(guest, mine):
    assert guest.post(f"/api/recordings/{mine.public_id}/links", json={}).status_code == 401
    assert guest.get("/api/shared-with-me").status_code == 401


def test_CSRF_헤더가_없으면_공유를_만들_수_없다(client, mine):
    del client.headers["x-csrf-token"]

    assert client.post(f"/api/recordings/{mine.public_id}/links", json={}).status_code == 403


def test_중지된_계정은_링크의_잠금을_우회하지_못한다(client, app, db, workspace, mine):
    """세션을 죽이는 흐름이 없으므로 중지된 사람의 브라우저 세션은 살아 있다.

    그 세션이 워크스페이스 역할로 링크의 잠금을 통째로 우회하고 있었다. 정상 라우트는
    403으로 막는데 공유 링크만 열려 있어서, 옛 구성원이 토큰 하나로 오디오와 실명에
    계속 닿았다.
    """
    token = make_link(
        client, mine, password=LINK_PASSWORD, allow_audio=False, allow_speaker_names=False
    )["token"]
    stopped = create_user(
        db,
        email="stopped@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="중지",
        status="active",
    )
    add_member(db, workspace, stopped, "member")
    db.commit()
    theirs = TestClient(app)
    login(theirs, stopped.email)
    stopped.status = "disabled"
    db.commit()

    assert theirs.get(f"/api/recordings/{mine.public_id}").status_code == 403
    assert theirs.get(f"/api/shared/{token}").status_code == 401

    theirs.post(f"/api/shared/{token}/unlock", json={"password": LINK_PASSWORD})
    body = theirs.get(f"/api/shared/{token}").json()
    assert body["speaker_names"] == {}
    assert body["allow_audio"] is False
    assert theirs.get(f"/api/shared/{token}/audio").status_code == 403


def test_공개_응답은_내부_상태를_그대로_내보내지_않는다(client, guest, db, mine):
    """quota_blocked를 그대로 주면 소유자의 한도 상태가 링크로 샌다."""
    token = make_link(client, mine)["token"]
    mine.status = "quota_blocked"
    db.commit()

    body = guest.get(f"/api/shared/{token}").json()

    assert body["status"] == "unavailable"


def test_처리_중이면_처리_중이라고만_말한다(client, guest, db, mine):
    """받는 사람은 전사가 끝나기 전에 연다. 어느 단계인지까지 알려줄 이유는 없다."""
    token = make_link(client, mine)["token"]
    mine.status = "summarizing"
    db.commit()

    assert guest.get(f"/api/shared/{token}").json()["status"] == "processing"
