from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from soriham_api import ingest
from soriham_api.ingest import (
    ingest_file,
    parse_recorded_at,
    partial_hash,
    resume_status,
    scan,
)
from soriham_api.models import Recording, Segment


@pytest.fixture(autouse=True)
def no_ffprobe(monkeypatch):
    monkeypatch.setattr(ingest, "probe_duration", lambda path: 12.3)


def write_wav(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_partial_hash_depends_on_content_and_size(tmp_path: Path):
    a = write_wav(tmp_path / "a.wav", b"x" * 100)
    b = write_wav(tmp_path / "b.wav", b"x" * 100)
    c = write_wav(tmp_path / "c.wav", b"y" * 100)
    assert partial_hash(a, 100) == partial_hash(b, 100)
    assert partial_hash(a, 100) != partial_hash(c, 100)


def test_partial_hash_reads_file_tail(tmp_path: Path):
    size = 3 * 1024 * 1024
    a = write_wav(tmp_path / "a.wav", b"x" * size)
    b = write_wav(tmp_path / "b.wav", b"x" * (size - 1) + b"z")
    assert partial_hash(a, size) != partial_hash(b, size)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("20260817_143000.wav", datetime(2026, 8, 17, 14, 30, 0)),
        ("260817_143000.m4a", datetime(2026, 8, 17, 14, 30, 0)),
        ("REC 2026-08-17 14.30.00.mp3", datetime(2026, 8, 17, 14, 30, 0)),
        ("meeting-20260817.wav", datetime(2026, 8, 17)),
        ("회의록.wav", None),
        ("999999_999999.wav", None),
    ],
)
def test_parse_recorded_at(filename: str, expected: datetime | None):
    got = parse_recorded_at(filename)
    if expected is None:
        assert got is None
    else:
        assert got is not None
        assert got.replace(tzinfo=None) == expected


def test_scan_registers_and_detects_duplicates(db, tmp_path: Path, workspace):
    write_wav(tmp_path / "rec" / "20260817_100000.wav", b"content-1")
    write_wav(tmp_path / "rec" / "sub/copy.wav", b"content-1")
    write_wav(tmp_path / "rec" / "other.m4a", b"content-2")
    write_wav(tmp_path / "rec" / "note.txt", b"not audio")

    stats = scan(db, (tmp_path / "rec",), workspace_id=workspace.id)
    assert stats == {"new": 2, "duplicate": 1, "reappeared": 0, "missing": 0}

    rows = db.scalars(select(Recording).order_by(Recording.id)).all()
    assert len(rows) == 3
    dup = next(r for r in rows if r.status == "duplicate")
    assert dup.duplicate_of_id is not None
    first = next(r for r in rows if r.filename == "20260817_100000.wav")
    assert first.status == "pending"
    assert first.duration_sec == 12.3
    assert first.recorded_at is not None

    # 재스캔은 아무것도 바꾸지 않는다
    assert scan(db, (tmp_path / "rec",), workspace_id=workspace.id) == {
        "new": 0,
        "duplicate": 0,
        "reappeared": 0,
        "missing": 0,
    }


def test_scan_marks_missing_and_reappeared(db, tmp_path: Path, workspace):
    target = write_wav(tmp_path / "rec" / "a.wav", b"gone")
    scan(db, (tmp_path / "rec",), workspace_id=workspace.id)

    target.unlink()
    assert scan(db, (tmp_path / "rec",), workspace_id=workspace.id)["missing"] == 1
    row = db.scalars(select(Recording)).one()
    assert row.status == "missing"

    write_wav(target, b"gone")
    assert scan(db, (tmp_path / "rec",), workspace_id=workspace.id)["reappeared"] == 1
    db.refresh(row)
    assert row.status == "pending"


def test_resume_status_uses_checkpoints(db, tmp_path: Path, workspace):
    path = write_wav(tmp_path / "rec" / "a.wav", b"x")
    scan(db, (tmp_path / "rec",), workspace_id=workspace.id)
    row = db.scalars(select(Recording)).one()

    assert resume_status(row) == "pending"
    row.segments.append(
        Segment(idx=0, start_sec=0.0, end_sec=1.0, text="안녕하세요", speaker_key=None)
    )
    db.flush()
    assert resume_status(row) == "enriching"
    row.summary = "요약"
    assert resume_status(row) == "done"

    # missing 후 재등장하면 체크포인트 기준 상태로 돌아온다
    path.unlink()
    scan(db, (tmp_path / "rec",), workspace_id=workspace.id)
    write_wav(path, b"x")
    scan(db, (tmp_path / "rec",), workspace_id=workspace.id)
    db.refresh(row)
    assert row.status == "done"


def test_스캔은_다른_워크스페이스의_녹음을_missing으로_바꾸지_않는다(
    db, tmp_path: Path, workspace, other_workspace
):
    """이 전환에서 가장 위험한 자리.

    다른 워크스페이스의 녹음이 **스캔 폴더 아래에 있고 파일이 없을 때** 스윕이
    그것까지 훑으면, 주인이 스캔 한 번 돌릴 때 남의 녹음이 조용히 "원본 없음"이 된다.
    경로가 겹치는 배치(업로드 폴더가 감시 폴더 안에 들어간 사고)에서 실제로 생긴다.
    """
    rec_dir = tmp_path / "rec"
    theirs = write_wav(rec_dir / "theirs.wav", b"theirs")
    ingest_file(db, theirs, workspace_id=other_workspace.id, source="upload")
    mine_uploaded = write_wav(rec_dir / "mine-uploaded.wav", b"mine-up")
    ingest_file(db, mine_uploaded, workspace_id=workspace.id, source="upload")
    db.commit()
    # 스캔이 보게 될 폴더에서 두 파일을 치운다 — 범위가 없으면 둘 다 missing이 된다
    theirs.unlink()
    mine_uploaded.unlink()

    write_wav(rec_dir / "a.wav", b"mine")
    scan(db, (rec_dir,), workspace_id=workspace.id)

    assert (
        db.scalar(select(Recording.status).where(Recording.filename == "theirs.wav")) != "missing"
    )
    # 같은 워크스페이스라도 업로드본은 주인의 스캔 폴더 사정과 무관하다
    assert (
        db.scalar(select(Recording.status).where(Recording.filename == "mine-uploaded.wav"))
        != "missing"
    )


def test_스캔은_자기_워크스페이스의_스캔본은_missing으로_바꾼다(db, tmp_path: Path, workspace):
    """범위를 좁히느라 원래 하던 일까지 멈추지 않았는지 본다."""
    rec_dir = tmp_path / "rec"
    gone = write_wav(rec_dir / "gone.wav", b"gone")
    scan(db, (rec_dir,), workspace_id=workspace.id)
    gone.unlink()

    assert scan(db, (rec_dir,), workspace_id=workspace.id)["missing"] == 1
    assert db.scalar(select(Recording.status).where(Recording.filename == "gone.wav")) == "missing"


def test_같은_파일이라도_워크스페이스가_다르면_중복이_아니다(
    db, tmp_path: Path, workspace, other_workspace
):
    """중복 판정이 전역이면 같은 파일을 올려보는 것만으로 남이 그걸 가졌는지 알 수 있다."""
    same = b"identical bytes"
    mine = write_wav(tmp_path / "mine" / "a.wav", same)
    theirs = write_wav(tmp_path / "theirs" / "a.wav", same)

    first = ingest_file(db, mine, workspace_id=workspace.id, source="upload")
    second = ingest_file(db, theirs, workspace_id=other_workspace.id, source="upload")
    db.commit()

    assert first.status == "pending"
    assert second.status == "pending"
    assert second.duplicate_of_id is None
