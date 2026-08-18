from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from soriham_api.app import create_app
from soriham_api.config import Settings
from soriham_api.models import Recording
from soriham_api.uploads import (
    MAX_NAME_BYTES,
    STAGING_DIRNAME,
    STALE_AGE_SEC,
    cleanup_staging,
    safe_filename,
)

WAV = b"RIFF" + b"\x00" * 2048


def make_client(engine, upload_dir: Path | None, max_mb: int = 4096) -> TestClient:
    settings = Settings(
        database_url="unused",
        audio_dirs=(),
        runner_url="http://runner.test",
        runner_upload=False,
        stt_model=None,
        stt_language=None,
        cors_origins=("http://localhost:5174",),
        upload_dir=upload_dir,
        max_upload_bytes=max_mb * 1024 * 1024,
        enrich_backend="off",
        ollama_url="http://localhost:11434",
        ollama_model="qwen3:8b",
    )
    return TestClient(create_app(settings=settings, session_factory=sessionmaker(bind=engine)))


def leftovers(upload_dir: Path) -> list[Path]:
    """스테이징에 남은 파일 (실패 경로에서 0이어야 한다)."""
    staging = upload_dir / STAGING_DIRNAME
    return [p for p in staging.iterdir() if p.is_file()] if staging.is_dir() else []


def test_업로드하면_저장되고_pending으로_등록된다(engine, db, tmp_path: Path):
    up = tmp_path / "uploads"
    client = make_client(engine, up)

    resp = client.post("/api/recordings", files={"file": ("20260817_143000.wav", WAV, "audio/wav")})

    assert resp.status_code == 201
    body = resp.json()
    assert body["filename"] == "20260817_143000.wav"
    assert body["status"] == "pending"

    recording = db.scalar(select(Recording).where(Recording.filename == "20260817_143000.wav"))
    assert recording is not None
    saved = Path(recording.path)
    assert saved.is_file()
    assert saved.read_bytes() == WAV
    # 파일명 날짜로 YYYY-MM 폴더에 들어간다
    assert saved.parent == up / "2026-08"
    assert leftovers(up) == []


def test_오디오가_아닌_확장자는_거부한다(engine, db, tmp_path: Path):
    up = tmp_path / "uploads"
    client = make_client(engine, up)

    resp = client.post("/api/recordings", files={"file": ("메모.txt", b"hello", "text/plain")})

    assert resp.status_code == 415
    assert leftovers(up) == []
    assert db.scalar(select(Recording)) is None


def test_크기_상한을_넘으면_413이고_잔여물이_없다(engine, db, tmp_path: Path):
    up = tmp_path / "uploads"
    client = make_client(engine, up, max_mb=1)

    big = b"\x00" * (2 * 1024 * 1024)
    resp = client.post("/api/recordings", files={"file": ("big.wav", big, "audio/wav")})

    assert resp.status_code == 413
    assert "1MB" in resp.json()["detail"]
    assert leftovers(up) == []
    assert db.scalar(select(Recording)) is None


def test_같은_파일을_다시_올리면_409이고_사본이_안_생긴다(engine, db, tmp_path: Path):
    up = tmp_path / "uploads"
    client = make_client(engine, up)

    first = client.post("/api/recordings", files={"file": ("회의.wav", WAV, "audio/wav")})
    assert first.status_code == 201

    again = client.post("/api/recordings", files={"file": ("회의-복사본.wav", WAV, "audio/wav")})

    assert again.status_code == 409
    detail = again.json()["detail"]
    assert detail["recording_id"] == first.json()["id"]
    assert leftovers(up) == []
    # 디스크에 남은 오디오는 첫 업로드 한 벌뿐
    assert [p.name for p in up.rglob("*.wav")] == ["회의.wav"]


def test_UPLOAD_DIR이_없으면_503(engine, db, tmp_path: Path):
    client = make_client(engine, None)

    resp = client.post("/api/recordings", files={"file": ("a.wav", WAV, "audio/wav")})

    assert resp.status_code == 503


def test_이름이_같으면_뒤에_번호를_붙여_저장한다(engine, db, tmp_path: Path):
    up = tmp_path / "uploads"
    client = make_client(engine, up)

    client.post("/api/recordings", files={"file": ("20260817_090000.wav", WAV, "audio/wav")})
    # 내용이 달라야 중복 판정에 걸리지 않는다
    client.post("/api/recordings", files={"file": ("20260817_090000.wav", WAV + b"x", "audio/wav")})

    names = sorted(p.name for p in (up / "2026-08").iterdir())
    assert names == ["20260817_090000-2.wav", "20260817_090000.wav"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../evil.wav", "evil.wav"),
        ("/etc/passwd.wav", "passwd.wav"),
        (r"D:\녹음\rec.wav", "rec.wav"),
        ("..", None),
        ("", None),
        (None, None),
    ],
)
def test_파일명에서_경로_성분을_제거한다(raw, expected):
    assert safe_filename(raw) == expected


def test_경로_탈출을_시도해도_보관_폴더_안에만_저장된다(engine, db, tmp_path: Path):
    up = tmp_path / "uploads"
    client = make_client(engine, up)

    resp = client.post("/api/recordings", files={"file": ("../../evil.wav", WAV, "audio/wav")})

    assert resp.status_code == 201
    recording = db.scalar(select(Recording))
    assert recording is not None
    assert Path(recording.path).is_relative_to(up.resolve())
    assert Path(recording.path).name == "evil.wav"


def test_빈_파일은_거부한다(engine, db, tmp_path: Path):
    up = tmp_path / "uploads"
    client = make_client(engine, up)

    resp = client.post("/api/recordings", files={"file": ("empty.wav", b"", "audio/wav")})

    assert resp.status_code == 422
    assert leftovers(up) == []
    assert db.scalar(select(Recording)) is None


def test_파일만_지워진_기존_녹음의_경로를_덮어쓰지_않는다(engine, db, tmp_path: Path):
    """DB에는 남아 있고 디스크에서만 사라진 경로가 있어도 새 업로드가 그 자리를
    차지하면 안 된다 — 그 행이 엉뚱한 오디오를 가리키게 된다."""
    up = tmp_path / "uploads"
    client = make_client(engine, up)

    first = client.post(
        "/api/recordings", files={"file": ("20260817_090000.wav", WAV, "audio/wav")}
    )
    assert first.status_code == 201
    original = db.scalar(select(Recording))
    assert original is not None
    Path(original.path).unlink()  # 사용자가 파일만 지운 상황

    resp = client.post(
        "/api/recordings", files={"file": ("20260817_090000.wav", WAV + b"other", "audio/wav")}
    )

    assert resp.status_code == 201
    assert not Path(original.path).exists()  # 기존 행의 경로는 비어 있는 채로 유지
    new = db.scalar(select(Recording).where(Recording.id != original.id))
    assert new is not None
    assert Path(new.path).name == "20260817_090000-2.wav"
    assert Path(new.path).read_bytes() == WAV + b"other"


def test_missing_행은_중복_판정에서_제외한다(engine, db, tmp_path: Path):
    """원본이 유실된 녹음의 백업본을 다시 올릴 수 있어야 한다."""
    up = tmp_path / "uploads"
    client = make_client(engine, up)

    first = client.post("/api/recordings", files={"file": ("잃어버린회의.wav", WAV, "audio/wav")})
    assert first.status_code == 201
    lost = db.scalar(select(Recording))
    assert lost is not None
    lost.status = "missing"
    db.commit()

    resp = client.post("/api/recordings", files={"file": ("복구본.wav", WAV, "audio/wav")})

    assert resp.status_code == 201
    assert resp.json()["status"] == "pending"


def test_이름이_너무_길면_잘라서_저장한다(engine, db, tmp_path: Path):
    up = tmp_path / "uploads"
    client = make_client(engine, up)

    long_name = "회" * 300 + ".wav"
    resp = client.post("/api/recordings", files={"file": (long_name, WAV, "audio/wav")})

    assert resp.status_code == 201
    saved = Path(db.scalar(select(Recording)).path)
    assert saved.is_file()
    # 리눅스(ext4)는 바이트로 세므로 문자 수가 아니라 인코딩 길이를 본다
    assert len(saved.name.encode()) <= MAX_NAME_BYTES
    assert saved.suffix == ".wav"


def test_널바이트_이름은_쓸_수_없는_이름으로_본다():
    assert safe_filename("a\x00b.wav") is None


def test_스테이징_정리는_오래된_것만_지운다(tmp_path: Path):
    """다른 프로세스가 받고 있는 중인 파일을 지우면 그 업로드가 깨진다."""
    up = tmp_path / "uploads"
    staging = up / STAGING_DIRNAME
    staging.mkdir(parents=True)
    fresh = staging / "fresh.part"
    stale = staging / "stale.part"
    fresh.write_bytes(b"x")
    stale.write_bytes(b"x")
    import os

    old = staging.stat().st_mtime - STALE_AGE_SEC - 60
    os.utime(stale, (old, old))

    cleanup_staging(up)

    assert fresh.exists()
    assert not stale.exists()
