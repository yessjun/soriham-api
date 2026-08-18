"""인제스트: 녹음 폴더 스캔과 DB 등록. 원본 파일은 읽기만 하고 제자리 인덱싱한다."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from soriham_api.models import Recording

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".wma",
    ".amr",
    ".awb",
    ".3gp",
}

_PARTIAL_CHUNK = 1024 * 1024  # 앞뒤 1MB

# 녹음기·녹음앱에서 흔한 파일명 날짜 패턴 (구체적인 것부터)
_DATETIME_PATTERNS = [
    # 20260817_143000, 2026-08-17 14.30.00, 2026-08-17-143000 등
    (r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})[-_. T]?(\d{2})[-_.:]?(\d{2})[-_.:]?(\d{2})", "full"),
    # 260817_143000 (2자리 연도)
    (r"\b(\d{2})(\d{2})(\d{2})[-_.](\d{2})(\d{2})(\d{2})\b", "yy"),
    # 날짜만: 20260817
    (r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", "date"),
]


def partial_hash(path: Path, size: int) -> str:
    """크기 + 앞뒤 1MB 해시 — 대용량 파일 전체를 읽지 않는 중복 감지 키."""
    h = hashlib.sha256()
    h.update(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(_PARTIAL_CHUNK))
        if size > _PARTIAL_CHUNK:
            f.seek(-_PARTIAL_CHUNK, 2)
            h.update(f.read(_PARTIAL_CHUNK))
    return h.hexdigest()


def parse_recorded_at(filename: str) -> datetime | None:
    """파일명에서 녹음 일시를 추출한다. 실패하면 None (로컬 타임존으로 해석)."""
    tz = datetime.now().astimezone().tzinfo
    for pattern, kind in _DATETIME_PATTERNS:
        m = re.search(pattern, filename)
        if not m:
            continue
        try:
            g = [int(x) for x in m.groups()]
            if kind == "yy":
                g[0] += 2000
            if kind == "date":
                candidate = datetime(g[0], g[1], g[2], tzinfo=tz)
            else:
                candidate = datetime(g[0], g[1], g[2], g[3], g[4], g[5], tzinfo=tz)
        except ValueError:
            continue
        if 2000 <= candidate.year <= datetime.now().year + 1:
            return candidate
    return None


def probe_duration(path: Path) -> float | None:
    """ffprobe로 오디오 길이(초)를 읽는다. 실패하면 None."""
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def resume_status(recording: Recording) -> str:
    """체크포인트(저장된 산출물)로부터 재개 지점 상태를 유도한다."""
    if recording.summary is not None:
        return "done"
    if recording.segments:
        return "enriching"
    return "pending"


def find_duplicate(session: Session, digest: str) -> Recording | None:
    """같은 부분 해시로 이미 등록된 원본을 찾는다.

    duplicate는 물론 missing도 제외한다 — 파일이 사라진 행을 원본으로 치면, 백업본을
    다시 넣으려 할 때 중복으로 막혀 복구할 방법이 없어진다.
    """
    return session.scalar(
        select(Recording)
        .where(
            Recording.partial_hash == digest,
            Recording.status.not_in(("duplicate", "missing")),
        )
        .order_by(Recording.id)
    )


def ingest_file(session: Session, path: Path) -> Recording | None:
    """파일 하나를 등록한다. 이미 등록된 경로면 재등장 처리만 하고 None을 돌려준다."""
    path = path.resolve()
    existing = session.scalar(select(Recording).where(Recording.path == str(path)))
    if existing is not None:
        if existing.status == "missing":
            existing.status = resume_status(existing)
            logger.info("재등장: %s -> %s", path.name, existing.status)
        return None

    size = path.stat().st_size
    digest = partial_hash(path, size)
    original = find_duplicate(session, digest)
    recording = Recording(
        path=str(path),
        filename=path.name,
        size_bytes=size,
        partial_hash=digest,
        recorded_at=parse_recorded_at(path.name),
        duration_sec=probe_duration(path),
        status="duplicate" if original is not None else "pending",
        duplicate_of_id=original.id if original is not None else None,
    )
    session.add(recording)
    return recording


def scan(session: Session, dirs: tuple[Path, ...]) -> dict[str, int]:
    """폴더들을 스캔해 신규 등록·재등장·유실을 반영하고 집계를 돌려준다."""
    stats = {"new": 0, "duplicate": 0, "reappeared": 0, "missing": 0}

    seen: set[str] = set()
    for base in dirs:
        if not base.is_dir():
            logger.warning("스캔 폴더가 없습니다: %s", base)
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            seen.add(str(path.resolve()))
            before = session.scalar(
                select(Recording.status).where(Recording.path == str(path.resolve()))
            )
            created = ingest_file(session, path)
            if created is not None:
                stats["duplicate" if created.status == "duplicate" else "new"] += 1
            elif before == "missing":
                stats["reappeared"] += 1

    # 스캔 폴더 아래로 등록돼 있던 파일이 사라졌으면 missing 마킹 (삭제하지 않는다)
    prefixes = tuple(str(d.resolve()) + os.sep for d in dirs if d.is_dir())
    for recording in session.scalars(select(Recording).where(Recording.status != "missing")):
        if recording.path.startswith(prefixes) and recording.path not in seen:
            recording.status = "missing"
            stats["missing"] += 1

    session.commit()
    return stats
