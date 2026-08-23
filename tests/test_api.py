from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from conftest import login, make_settings
from soriham_api.app import create_app
from soriham_api.ingest import scan
from soriham_api.models import JobLog, Recording
from soriham_api.worker import process_one
from test_worker import RESULT, FakeRunnerClient


@pytest.fixture
def client(engine, db, owner, tmp_path: Path):
    # 스캔본의 오디오를 내보내려면 감시 폴더가 설정에 있어야 한다 — 없으면 경로
    # 봉쇄 검사가 막는다
    app = create_app(
        settings=make_settings(audio_dirs=(tmp_path / "rec",)),
        session_factory=sessionmaker(bind=engine),
    )
    c = TestClient(app)
    login(c, owner.email)
    return c


@pytest.fixture
def anon_client(engine, db):
    app = create_app(settings=make_settings(), session_factory=sessionmaker(bind=engine))
    return TestClient(app)


def ws_path(workspace, suffix: str) -> str:
    return f"/api/workspaces/{workspace.public_id}/{suffix}"


def make_recording(db, tmp_path: Path, workspace, name: str = "20260817_100000.wav") -> Recording:
    p = tmp_path / "rec" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"RIFF" + name.encode() + b"\x00" * 64)
    scan(db, (tmp_path / "rec",), workspace_id=workspace.id)
    process_one(db, FakeRunnerClient())
    return db.scalars(select(Recording).where(Recording.filename == name)).one()


def test_list_and_detail(client, db, tmp_path: Path, workspace):
    rec = make_recording(db, tmp_path, workspace)

    body = client.get(ws_path(workspace, "recordings")).json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == str(rec.public_id)
    assert item["status"] == "done"
    assert "path" not in item  # 로컬 절대경로 비노출

    detail = client.get(f"/api/recordings/{rec.public_id}").json()
    assert [s["text"] for s in detail["segments"]] == ["안녕하세요", "반갑습니다"]
    assert detail["segments"][0]["speaker_key"] == "SPEAKER_00"
    assert detail["stt_meta"]["device"] == RESULT["meta"]["device"]


def test_detail_404_for_unknown_uuid(client):
    assert client.get("/api/recordings/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/api/recordings/123").status_code == 422  # 정수 내부 id 거부


def test_speaker_rename_roundtrip(client, db, tmp_path: Path, workspace):
    rec = make_recording(db, tmp_path, workspace)
    resp = client.put(
        f"/api/recordings/{rec.public_id}/speakers/SPEAKER_00", json={"name": "김소리"}
    )
    assert resp.status_code == 200
    detail = client.get(f"/api/recordings/{rec.public_id}").json()
    assert detail["speaker_names"] == {"SPEAKER_00": "김소리"}

    client.put(f"/api/recordings/{rec.public_id}/speakers/SPEAKER_00", json={"name": "박소리"})
    detail = client.get(f"/api/recordings/{rec.public_id}").json()
    assert detail["speaker_names"] == {"SPEAKER_00": "박소리"}


def test_tags_attach_and_filter(client, db, tmp_path: Path, workspace):
    rec = make_recording(db, tmp_path, workspace)
    other = make_recording(db, tmp_path, workspace, "20260816_090000.wav")

    tags = client.post(f"/api/recordings/{rec.public_id}/tags", json={"name": "주간회의"}).json()
    assert [t["name"] for t in tags] == ["주간회의"]
    tag_id = tags[0]["id"]

    # 같은 이름은 재사용되고 다른 녹음에도 붙는다
    client.post(f"/api/recordings/{other.public_id}/tags", json={"name": "주간회의"})
    assert len(client.get(ws_path(workspace, "tags")).json()) == 1

    filtered = client.get(ws_path(workspace, "recordings"), params={"tag": tag_id}).json()
    assert filtered["total"] == 2

    client.delete(f"/api/recordings/{rec.public_id}/tags/{tag_id}")
    filtered = client.get(ws_path(workspace, "recordings"), params={"tag": tag_id}).json()
    assert filtered["total"] == 1


def test_search_returns_segment_hits(client, db, tmp_path: Path, workspace):
    rec = make_recording(db, tmp_path, workspace)

    hits = client.get(ws_path(workspace, "search"), params={"q": "안녕"}).json()["hits"]
    assert len(hits) == 1
    assert hits[0]["recording"]["id"] == str(rec.public_id)
    assert hits[0]["segment"]["text"] == "안녕하세요"
    assert hits[0]["segment"]["start_sec"] == 0.0

    # 파일명 매칭은 segment 없이 반환
    hits = client.get(ws_path(workspace, "search"), params={"q": "20260817"}).json()["hits"]
    assert hits[0]["segment"] is None

    assert client.get(ws_path(workspace, "search"), params={"q": "없는말"}).json()["hits"] == []


def test_list_q_filter_matches_segment_text(client, db, tmp_path: Path, workspace):
    make_recording(db, tmp_path, workspace)
    make_recording(db, tmp_path, workspace, "20260101_000000.wav")

    body = client.get(ws_path(workspace, "recordings"), params={"q": "반갑"}).json()
    assert body["total"] == 2  # 두 녹음 모두 같은 가짜 전사 결과

    body = client.get(ws_path(workspace, "recordings"), params={"q": "20260101"}).json()
    assert body["total"] == 1


def test_audio_range_streaming(client, db, tmp_path: Path, workspace):
    rec = make_recording(db, tmp_path, workspace)
    content = Path(rec.path).read_bytes()

    full = client.get(f"/api/recordings/{rec.public_id}/audio")
    assert full.status_code == 200
    assert full.content == content
    assert full.headers["accept-ranges"] == "bytes"

    part = client.get(f"/api/recordings/{rec.public_id}/audio", headers={"Range": "bytes=4-11"})
    assert part.status_code == 206
    assert part.content == content[4:12]
    assert part.headers["content-range"] == f"bytes 4-11/{len(content)}"

    tail = client.get(f"/api/recordings/{rec.public_id}/audio", headers={"Range": "bytes=-4"})
    assert tail.status_code == 206
    assert tail.content == content[-4:]

    bad = client.get(f"/api/recordings/{rec.public_id}/audio", headers={"Range": "bytes=999999-"})
    assert bad.status_code == 416


def test_audio_missing_file_404(client, db, tmp_path: Path, workspace):
    rec = make_recording(db, tmp_path, workspace)
    Path(rec.path).unlink()
    assert client.get(f"/api/recordings/{rec.public_id}/audio").status_code == 404


def test_stats(client, db, tmp_path: Path, workspace):
    make_recording(db, tmp_path, workspace)
    # 실측 로그 기반 배속·ETA
    log = db.scalars(select(JobLog)).one()
    log.audio_sec = 10.0
    log.elapsed_sec = 2.0
    rec2 = make_recording(db, tmp_path, workspace, "20260816_090000.wav")
    rec2.status = "error"
    rec2.error = "stt: 러너 다운"
    db.commit()

    body = client.get(ws_path(workspace, "stats")).json()
    statuses = {x["status"]: x["count"] for x in body["by_status"]}
    assert statuses == {"done": 1, "error": 1}
    assert body["speed_ratio"] is not None
    assert body["recent_errors"][0]["id"] == str(rec2.public_id)


def test_진행률과_남은_시간이_응답에_실린다(client, db, tmp_path: Path, workspace) -> None:
    recording = make_recording(db, tmp_path, workspace, "20260818_150000.wav")
    recording.status = "transcribing"
    recording.progress = 0.25
    recording.stage_started_at = datetime.now(UTC) - timedelta(seconds=60)
    db.commit()

    body = client.get(f"/api/recordings/{recording.public_id}").json()

    assert body["progress"] == 0.25
    # 25%에 60초 걸렸으면 남은 75%는 약 180초
    assert 170 < body["eta_sec"] < 190


def test_진행률이_없으면_남은_시간도_없다(client, db, tmp_path: Path, workspace) -> None:
    recording = make_recording(db, tmp_path, workspace, "20260818_160000.wav")

    body = client.get(f"/api/recordings/{recording.public_id}").json()

    assert body["progress"] is None
    assert body["eta_sec"] is None


def test_진행률이_0이면_남은_시간을_추정하지_않는다(client, db, tmp_path: Path, workspace) -> None:
    """0으로 나누지 않고, 근거 없는 추정을 내놓지도 않는다."""
    recording = make_recording(db, tmp_path, workspace, "20260818_170000.wav")
    recording.status = "transcribing"
    recording.progress = 0.0
    recording.stage_started_at = datetime.now(UTC) - timedelta(seconds=30)
    db.commit()

    body = client.get(f"/api/recordings/{recording.public_id}").json()

    assert body["eta_sec"] is None
