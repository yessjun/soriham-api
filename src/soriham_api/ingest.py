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


def find_duplicate(session: Session, digest: str, *, workspace_id: int) -> Recording | None:
    """같은 워크스페이스 안에서 같은 부분 해시로 등록된 원본을 찾는다.

    duplicate는 물론 missing도 제외한다 — 파일이 사라진 행을 원본으로 치면, 백업본을
    다시 넣으려 할 때 중복으로 막혀 복구할 방법이 없어진다.

    범위를 워크스페이스로 좁히는 이유: 전역으로 찾으면 같은 파일을 올려보는 것만으로
    남이 그 파일을 가졌는지, 무슨 이름을 붙였는지 알 수 있다.
    """
    return session.scalar(
        select(Recording)
        .where(
            Recording.workspace_id == workspace_id,
            Recording.partial_hash == digest,
            Recording.status.not_in(("duplicate", "missing")),
        )
        .order_by(Recording.id)
    )


def ingest_file(
    session: Session,
    path: Path,
    *,
    workspace_id: int,
    source: str = "scan",
    created_by_user_id: int | None = None,
) -> Recording | None:
    """파일 하나를 등록한다. 이미 등록된 경로면 재등장 처리만 하고 None을 돌려준다.

    `workspace_id`는 기본값 없는 키워드 인자다 — 호출부가 어느 워크스페이스에 넣는지
    반드시 말하게 한다. 기본값을 두면 빠뜨린 호출부가 조용히 엉뚱한 곳에 넣는다.
    """
    path = path.resolve()
    existing = session.scalar(select(Recording).where(Recording.path == str(path)))
    if existing is not None:
        if existing.status == "missing":
            existing.status = resume_status(existing)
            logger.info("재등장: %s -> %s", path.name, existing.status)
        return None

    size = path.stat().st_size
    digest = partial_hash(path, size)
    original = find_duplicate(session, digest, workspace_id=workspace_id)
    recording = Recording(
        workspace_id=workspace_id,
        source=source,
        created_by_user_id=created_by_user_id,
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


# 스캔 도중 몇 건마다 커밋할지. 1만 개짜리 폴더를 한 트랜잭션으로 몰면 파일마다 도는
# ffprobe 때문에 수십 분이 걸리고, 그 사이 끊기면 등록이 통째로 사라진다
SCAN_COMMIT_EVERY = 200
# 이번 스캔이 파일을 하나도 못 봤을 때, 몇 건 넘게 사라진 것으로 보이면 디스크가
# 없는 것으로 보고 유실 판정을 건너뛴다
SWEEP_BLACKOUT_MIN = 20


def scan(session: Session, dirs: tuple[Path, ...], *, workspace_id: int) -> dict[str, int]:
    """폴더들을 스캔해 신규 등록·재등장·유실을 반영하고 집계를 돌려준다."""
    stats = {"new": 0, "duplicate": 0, "reappeared": 0, "missing": 0}

    since_commit = 0
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
            created = ingest_file(session, path, workspace_id=workspace_id)
            if created is not None:
                stats["duplicate" if created.status == "duplicate" else "new"] += 1
            elif before == "missing":
                stats["reappeared"] += 1
            since_commit += 1
            if since_commit >= SCAN_COMMIT_EVERY:
                session.commit()
                since_commit = 0

    # 스캔 폴더 아래로 등록돼 있던 파일이 사라졌으면 missing 마킹 (삭제하지 않는다).
    #
    # 범위를 좁히는 두 조건이 없으면 스캔 한 번에 **다른 사람의 업로드가 전부** missing이
    # 된다 — 그들의 파일은 이 폴더에 있을 이유가 없으므로 전부 "사라진 것"으로 보인다.
    # source까지 보는 이유: 스캔 워크스페이스에 섞여 들어온 업로드본도 지켜야 한다.
    #
    prefixes = tuple(str(d.resolve()) + os.sep for d in dirs if d.is_dir())
    gone = [
        recording
        for recording in session.scalars(
            select(Recording).where(
                Recording.status != "missing",
                Recording.workspace_id == workspace_id,
                Recording.source == "scan",
            )
        )
        if recording.path.startswith(prefixes) and recording.path not in seen
    ]
    # 이번 스캔에서 오디오를 하나도 못 봤는데 지워야 할 것이 무더기라면 폴더가 빈 것이
    # 아니라 디스크가 없는 것이다. 마운트가 풀린 자리는 빈 폴더로 남아 is_dir()가
    # 참이므로 이 구분을 다른 데서 할 수 없다. 사람이 손으로 지운 몇 건은 그대로 반영하고,
    # 무더기만 막는다 — 문턱은 어림값이고, 넘겨 놓친 것은 다음 스캔이 잡는다
    if not seen and len(gone) > SWEEP_BLACKOUT_MIN:
        logger.warning(
            "오디오를 하나도 못 봤는데 %d건이 사라진 것으로 보임 — 유실 판정을 건너뜀", len(gone)
        )
        gone = []
    for recording in gone:
        recording.status = "missing"
        stats["missing"] += 1
        since_commit += 1
        if since_commit >= SCAN_COMMIT_EVERY:
            session.commit()
            since_commit = 0

    session.commit()
    return stats
