from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from soriham_api.app import create_app
from soriham_api.config import Settings
from soriham_api.ingest import scan
from soriham_api.models import JobLog, Recording
from soriham_api.worker import process_one
from test_worker import RESULT, FakeRunnerClient


@pytest.fixture
def client(engine, db, tmp_path: Path):
    settings = Settings(
        database_url="unused",
        audio_dirs=(),
        runner_url="http://runner.test",
        runner_upload=False,
        stt_model=None,
        stt_language=None,
        enrich_backend="off",
        ollama_url="http://localhost:11434",
        ollama_model="qwen3:8b",
    )
    app = create_app(settings=settings, session_factory=sessionmaker(bind=engine))
    return TestClient(app)


def make_recording(db, tmp_path: Path, name: str = "20260817_100000.wav") -> Recording:
    p = tmp_path / "rec" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"RIFF" + name.encode() + b"\x00" * 64)
    scan(db, (tmp_path / "rec",))
    process_one(db, FakeRunnerClient())
    return db.scalars(select(Recording).where(Recording.filename == name)).one()


def test_list_and_detail(client, db, tmp_path: Path):
    rec = make_recording(db, tmp_path)

    body = client.get("/api/recordings").json()
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


def test_speaker_rename_roundtrip(client, db, tmp_path: Path):
    rec = make_recording(db, tmp_path)
    resp = client.put(
        f"/api/recordings/{rec.public_id}/speakers/SPEAKER_00", json={"name": "김소리"}
    )
    assert resp.status_code == 200
    detail = client.get(f"/api/recordings/{rec.public_id}").json()
    assert detail["speaker_names"] == {"SPEAKER_00": "김소리"}

    client.put(f"/api/recordings/{rec.public_id}/speakers/SPEAKER_00", json={"name": "박소리"})
    detail = client.get(f"/api/recordings/{rec.public_id}").json()
    assert detail["speaker_names"] == {"SPEAKER_00": "박소리"}


def test_tags_attach_and_filter(client, db, tmp_path: Path):
    rec = make_recording(db, tmp_path)
    other = make_recording(db, tmp_path, "20260816_090000.wav")

    tags = client.post(f"/api/recordings/{rec.public_id}/tags", json={"name": "주간회의"}).json()
    assert [t["name"] for t in tags] == ["주간회의"]
    tag_id = tags[0]["id"]

    # 같은 이름은 재사용되고 다른 녹음에도 붙는다
    client.post(f"/api/recordings/{other.public_id}/tags", json={"name": "주간회의"})
    assert len(client.get("/api/tags").json()) == 1

    filtered = client.get("/api/recordings", params={"tag": tag_id}).json()
    assert filtered["total"] == 2

    client.delete(f"/api/recordings/{rec.public_id}/tags/{tag_id}")
    filtered = client.get("/api/recordings", params={"tag": tag_id}).json()
    assert filtered["total"] == 1


def test_search_returns_segment_hits(client, db, tmp_path: Path):
    rec = make_recording(db, tmp_path)

    hits = client.get("/api/search", params={"q": "안녕"}).json()["hits"]
    assert len(hits) == 1
    assert hits[0]["recording"]["id"] == str(rec.public_id)
    assert hits[0]["segment"]["text"] == "안녕하세요"
    assert hits[0]["segment"]["start_sec"] == 0.0

    # 파일명 매칭은 segment 없이 반환
    hits = client.get("/api/search", params={"q": "20260817"}).json()["hits"]
    assert hits[0]["segment"] is None

    assert client.get("/api/search", params={"q": "없는말"}).json()["hits"] == []


def test_list_q_filter_matches_segment_text(client, db, tmp_path: Path):
    make_recording(db, tmp_path)
    make_recording(db, tmp_path, "20260101_000000.wav")

    body = client.get("/api/recordings", params={"q": "반갑"}).json()
    assert body["total"] == 2  # 두 녹음 모두 같은 가짜 전사 결과

    body = client.get("/api/recordings", params={"q": "20260101"}).json()
    assert body["total"] == 1


def test_audio_range_streaming(client, db, tmp_path: Path):
    rec = make_recording(db, tmp_path)
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


def test_audio_missing_file_404(client, db, tmp_path: Path):
    rec = make_recording(db, tmp_path)
    Path(rec.path).unlink()
    assert client.get(f"/api/recordings/{rec.public_id}/audio").status_code == 404


def test_stats(client, db, tmp_path: Path):
    make_recording(db, tmp_path)
    # 실측 로그 기반 배속·ETA
    log = db.scalars(select(JobLog)).one()
    log.audio_sec = 10.0
    log.elapsed_sec = 2.0
    rec2 = make_recording(db, tmp_path, "20260816_090000.wav")
    rec2.status = "error"
    rec2.error = "stt: 러너 다운"
    db.commit()

    body = client.get("/api/stats").json()
    statuses = {x["status"]: x["count"] for x in body["by_status"]}
    assert statuses == {"done": 1, "error": 1}
    assert body["speed_ratio"] is not None
    assert body["recent_errors"][0]["id"] == str(rec2.public_id)
