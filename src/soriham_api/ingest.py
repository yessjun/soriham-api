"""인제스트: 녹음 폴더 스캔과 DB 등록. 원본 파일은 읽기만 하고 제자리 인덱싱한다."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from soriham_api.files import content_hash, file_signature
from soriham_api.models import Recording

logger = logging.getLogger(__name__)
SOURCE_CHANGED_PREFIX = "원본 변경: "


class FileChangedDuringRead(RuntimeError):
    """등록용 정보를 읽는 동안 파일이 바뀌어 다시 확인해야 한다."""


def _ingest_lock(session: Session, key: str) -> None:
    lock_id = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
    session.execute(select(func.pg_advisory_xact_lock(lock_id)))


def _has_matching_file(recording: Recording) -> bool:
    if not recording.path_current:
        return False
    path = Path(recording.path)
    try:
        before = file_signature(path)
        if recording.file_signature == list(before):
            return True
        return content_hash(path) == recording.content_hash and file_signature(path) == before
    except OSError:
        return False


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
    if recording.duplicate_of_id is not None:
        return "duplicate"
    if recording.summary is not None:
        return "done"
    if recording.stt_meta is not None or recording.segments:
        return "enriching"
    return "pending"


def find_by_content(session: Session, full: str, *, workspace_id: int) -> list[Recording]:
    """같은 워크스페이스에서 같은 내용으로 등록된 행들. 중복 표시된 행은 뺀다.

    범위를 워크스페이스로 좁히는 이유: 전역으로 찾으면 같은 파일을 올려보는 것만으로
    남이 그 파일을 가졌는지, 무슨 이름을 붙였는지 알 수 있다.
    """
    return list(
        session.scalars(
            select(Recording)
            .where(
                Recording.workspace_id == workspace_id,
                Recording.content_hash == full,
                Recording.status != "duplicate",
                Recording.duplicate_of_id.is_(None),
            )
            .order_by(Recording.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


def find_duplicate(session: Session, digest: str, *, workspace_id: int) -> Recording | None:
    """전체 해시가 없던 시절 행과의 중복 판정. 부분 해시로만 본다.

    백필이 끝나면 도달하지 않는 경로다.
    """
    return session.scalar(
        select(Recording)
        .where(
            Recording.workspace_id == workspace_id,
            Recording.partial_hash == digest,
            Recording.content_hash.is_(None),
            Recording.path_current.is_(True),
            Recording.status.not_in(("duplicate", "missing")),
        )
        .order_by(Recording.id)
    )


def find_original(
    session: Session, *, workspace_id: int, full: str, digest: str
) -> Recording | None:
    """같은 내용이 이미 등록돼 있나. 중복 판정의 단일 창구다.

    파일이 실제로 있는 행만 원본이 된다. missing 행을 원본으로 치면 백업본을 다시
    넣으려 할 때 중복으로 막혀 복구할 방법이 없어진다.
    """
    for row in find_by_content(session, full, workspace_id=workspace_id):
        if row.status != "missing" and _has_matching_file(row):
            return row
    return find_duplicate(session, digest, workspace_id=workspace_id)


def find_moved(session: Session, full: str, *, workspace_id: int) -> Recording | None:
    """같은 내용으로 등록됐는데 그 경로에 파일이 없는 행 — 개명·이동의 반대편이다."""
    for row in find_by_content(session, full, workspace_id=workspace_id):
        if not _has_matching_file(row):
            return row
    return None


# ingest_file의 결과. 호출부가 집계와 로그를 이걸로 가른다
Outcome = Literal["new", "duplicate", "reappeared", "moved", "existing"]


def ingest_file(
    session: Session,
    path: Path,
    *,
    workspace_id: int,
    source: str = "scan",
    created_by_user_id: int | None = None,
    verify_existing: bool = False,
) -> tuple[Recording | None, Outcome]:
    """파일 하나를 등록하고 무엇을 했는지 함께 돌려준다.

    결과를 말로 돌려주는 이유: 호출부마다 "행이 없으면 무슨 일이 있었나"를 다시
    조회해서 알아내고 있었고, 판정 규칙이 그만큼 갈라졌다.

    `workspace_id`는 기본값 없는 키워드 인자다 — 호출부가 어느 워크스페이스에 넣는지
    반드시 말하게 한다. 기본값을 두면 빠뜨린 호출부가 조용히 엉뚱한 곳에 넣는다.
    """
    path = path.resolve()
    signature = file_signature(path)
    query = select(Recording).where(Recording.path == str(path), Recording.path_current.is_(True))
    existing = session.scalar(query)
    if existing is not None:
        if existing.workspace_id != workspace_id or existing.source != source:
            raise ValueError("이 경로는 다른 워크스페이스 또는 유입 경로에 등록돼 있습니다")
        if (
            not verify_existing
            and existing.content_hash is not None
            and existing.file_signature == list(signature)
            and existing.status != "missing"
        ):
            return existing, "existing"

    size = signature[2]
    digest = partial_hash(path, size)
    full = content_hash(path)
    duration = probe_duration(path)
    if file_signature(path) != signature:
        raise FileChangedDuringRead("파일을 읽는 동안 내용이 변경됐습니다")

    # 다른 이벤트와 스캔이 같은 내용·경로를 동시에 새 원본으로 등록하지 않는다
    _ingest_lock(session, f"content:{workspace_id}:{full}")
    _ingest_lock(session, f"path:{path}")
    existing = session.scalar(query.with_for_update().execution_options(populate_existing=True))
    if file_signature(path) != signature:
        raise FileChangedDuringRead("등록 대기 중 파일이 변경됐습니다")
    if existing is not None:
        if existing.workspace_id != workspace_id or existing.source != source:
            raise ValueError("이 경로는 다른 워크스페이스 또는 유입 경로에 등록돼 있습니다")
        same = existing.content_hash == full or (
            existing.content_hash is None and existing.partial_hash == digest
        )
        if same:
            existing.file_signature = list(signature)
            existing.content_hash = full
            existing.size_bytes = size
            existing.duration_sec = duration
            if existing.status == "missing":
                existing.status = resume_status(existing)
                return existing, "reappeared"
            if (existing.error or "").startswith(SOURCE_CHANGED_PREFIX):
                existing.status = resume_status(existing)
                existing.error = None
            return existing, "existing"
        if source != "scan":
            raise ValueError("업로드 원본의 내용을 같은 경로에서 교체할 수 없습니다")
        # 예전 공유가 새 파일을 가리키지 않도록 행과 산출물을 보존하며 경로만 놓는다
        existing.path_current = False
        existing.status = "missing"
        existing.runner_request_id = None
        existing.runner_started_at = None
        existing.progress = None
        existing.stage_started_at = None
        session.flush()

    # 같은 내용의 행이 있는데 그 파일이 사라졌다면 중복이 아니라 이동이다. 새 행을
    # 만들면 녹취록은 사라진 행에 남고 실물에는 중복 표시가 붙어, 검색으로 찾아
    # 들어간 쪽에서 재생이 안 된다
    moved = find_moved(session, full, workspace_id=workspace_id)
    if moved is not None:
        logger.info("경로 이동: %s -> %s", moved.filename, path.name)
        moved.path = str(path)
        moved.filename = path.name
        moved.size_bytes = size
        moved.partial_hash = digest
        moved.path_current = True
        moved.file_signature = list(signature)
        moved.duration_sec = duration
        # 새 이름에 날짜가 있으면 그것을 쓰고, 없으면 알던 값을 지킨다
        moved.recorded_at = parse_recorded_at(path.name) or moved.recorded_at
        moved.status = resume_status(moved)
        return moved, "moved"

    original = find_original(session, workspace_id=workspace_id, full=full, digest=digest)
    recording = Recording(
        workspace_id=workspace_id,
        source=source,
        created_by_user_id=created_by_user_id,
        path=str(path),
        filename=path.name,
        size_bytes=size,
        partial_hash=digest,
        content_hash=full,
        path_current=True,
        file_signature=list(signature),
        recorded_at=parse_recorded_at(path.name),
        duration_sec=duration,
        status="duplicate" if original is not None else "pending",
        duplicate_of_id=original.id if original is not None else None,
    )
    session.add(recording)
    return recording, ("duplicate" if original is not None else "new")


def backfill_content_hashes(
    session: Session, *, workspace_id: int | None = None, limit: int | None = None
) -> dict[str, int]:
    """전체 해시가 빈 행을 채운다. 컬럼이 생기기 전에 등록된 녹음이 대상이다.

    파일을 통째로 읽으므로 한 번에 다 돌리기 부담스러우면 `limit`으로 나눠 돌린다.
    파일이 없는 행은 건너뛴다 — 드라이브를 다시 붙이고 나서 채우면 된다.
    """
    query = (
        select(Recording)
        .where(Recording.content_hash.is_(None), Recording.path_current.is_(True))
        .order_by(Recording.id)
    )
    if workspace_id is not None:
        query = query.where(Recording.workspace_id == workspace_id)
    remaining = session.scalar(select(func.count()).select_from(query.subquery()))
    if limit is not None:
        query = query.limit(limit)

    filled = 0
    missing = 0
    for recording in session.scalars(query):
        path = Path(recording.path)
        if not path.is_file():
            missing += 1
            continue
        recording.content_hash = content_hash(path)
        filled += 1
        if filled % SCAN_COMMIT_EVERY == 0:
            session.commit()
    session.commit()
    return {"filled": filled, "missing": missing, "remaining": (remaining or 0) - filled}


# 스캔 도중 몇 건마다 커밋할지. 1만 개짜리 폴더를 한 트랜잭션으로 몰면 파일마다 도는
# ffprobe 때문에 수십 분이 걸리고, 그 사이 끊기면 등록이 통째로 사라진다
SCAN_COMMIT_EVERY = 200
# 이번 스캔이 파일을 하나도 못 봤을 때, 몇 건 넘게 사라진 것으로 보이면 디스크가
# 없는 것으로 보고 유실 판정을 건너뛴다
SWEEP_BLACKOUT_MIN = 20


def scan(session: Session, dirs: tuple[Path, ...], *, workspace_id: int) -> dict[str, int]:
    """폴더별 읽기 성공과 장치 정보를 확인한 뒤 등록·유실을 반영한다."""
    stats = {"new": 0, "duplicate": 0, "reappeared": 0, "moved": 0, "missing": 0}
    roots = sorted({base.resolve() for base in dirs})
    seen: set[str] = set()
    complete: set[Path] = set()
    identities: dict[Path, tuple[int, int]] = {}

    def owner(path: str) -> Path | None:
        matches = [base for base in roots if Path(path).is_relative_to(base)]
        return max(matches, key=lambda base: len(base.parts)) if matches else None

    def walk_error(error: OSError) -> None:
        raise error

    for base in roots:
        try:
            before = base.stat()
            if not base.is_dir():
                continue
            identities[base] = (before.st_dev, before.st_ino)
            for directory, _, names in os.walk(base, onerror=walk_error):
                for name in sorted(names):
                    path = Path(directory) / name
                    if path.suffix.lower() not in AUDIO_EXTENSIONS:
                        continue
                    resolved = path.resolve()
                    if not resolved.is_relative_to(base) or str(resolved) in seen:
                        continue
                    if not stat.S_ISREG(path.stat().st_mode):
                        continue
                    seen.add(str(resolved))
                    try:
                        _, outcome = ingest_file(
                            session, resolved, workspace_id=workspace_id, verify_existing=True
                        )
                        # 파일 단위로 확정해 다른 스캔과 내용 잠금을 서로 물고 기다리지 않는다
                        session.commit()
                        if outcome != "existing":
                            stats[outcome] += 1
                    except Exception:  # noqa: BLE001 - 파일 하나의 실패가 다른 등록을 끊지 않게 한다
                        session.rollback()
                        logger.exception("스캔 등록 실패: %s", path.name)
            after = base.stat()
            if identities[base] == (after.st_dev, after.st_ino):
                complete.add(base)
        except OSError:
            logger.warning("스캔 폴더를 끝까지 읽지 못해 유실 판정을 보류: %s", base)

    gone_by_root: dict[Path, list[int]] = {base: [] for base in roots}
    stored = session.execute(
        select(Recording.id, Recording.path, Recording.file_signature).where(
            Recording.path_current.is_(True),
            Recording.status != "missing",
            Recording.workspace_id == workspace_id,
            Recording.source == "scan",
        )
    )
    for row_id, path, signature in stored:
        owner_root = owner(path)
        if owner_root not in complete or path in seen:
            continue
        assert owner_root is not None
        if signature:
            parent = Path(path).parent
            device = None
            while parent.is_relative_to(owner_root):
                try:
                    device = parent.stat().st_dev
                    break
                except FileNotFoundError:
                    if parent == owner_root:
                        break
                    parent = parent.parent
                except OSError:
                    break
            if device != signature[0]:
                logger.warning("등록 때의 장치가 보이지 않아 유실 판정을 보류: %s", path)
                continue
        gone_by_root[owner_root].append(row_id)

    for base, gone in gone_by_root.items():
        has_seen = any(owner(path) == base for path in seen)
        if not has_seen and len(gone) > SWEEP_BLACKOUT_MIN:
            logger.warning("폴더에 오디오 없이 %d건이 사라져 유실 판정을 보류: %s", len(gone), base)
            continue
        for start in range(0, len(gone), SCAN_COMMIT_EVERY):
            chunk = gone[start : start + SCAN_COMMIT_EVERY]
            session.execute(
                update(Recording).where(Recording.id.in_(chunk)).values(status="missing")
            )
            stats["missing"] += len(chunk)
            session.commit()
    session.commit()
    return stats
