from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from soriham_api import ingest
from soriham_api.ingest import parse_recorded_at, partial_hash, resume_status, scan
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


def test_scan_registers_and_detects_duplicates(db, tmp_path: Path):
    write_wav(tmp_path / "rec" / "20260817_100000.wav", b"content-1")
    write_wav(tmp_path / "rec" / "sub/copy.wav", b"content-1")
    write_wav(tmp_path / "rec" / "other.m4a", b"content-2")
    write_wav(tmp_path / "rec" / "note.txt", b"not audio")

    stats = scan(db, (tmp_path / "rec",))
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
    assert scan(db, (tmp_path / "rec",)) == {
        "new": 0,
        "duplicate": 0,
        "reappeared": 0,
        "missing": 0,
    }


def test_scan_marks_missing_and_reappeared(db, tmp_path: Path):
    target = write_wav(tmp_path / "rec" / "a.wav", b"gone")
    scan(db, (tmp_path / "rec",))

    target.unlink()
    assert scan(db, (tmp_path / "rec",))["missing"] == 1
    row = db.scalars(select(Recording)).one()
    assert row.status == "missing"

    write_wav(target, b"gone")
    assert scan(db, (tmp_path / "rec",))["reappeared"] == 1
    db.refresh(row)
    assert row.status == "pending"


def test_resume_status_uses_checkpoints(db, tmp_path: Path):
    path = write_wav(tmp_path / "rec" / "a.wav", b"x")
    scan(db, (tmp_path / "rec",))
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
    scan(db, (tmp_path / "rec",))
    write_wav(path, b"x")
    scan(db, (tmp_path / "rec",))
    db.refresh(row)
    assert row.status == "done"
