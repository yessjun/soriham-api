"""콘솔 업로드 파일의 디스크 저장. DB 등록은 인제스트가 그대로 맡는다.

업로드는 "원본이 아직 서버에 없는" 경우라 한 번만 저장하고, 그 뒤로는 다른 녹음과
똑같이 제자리 인덱싱된다. 저장 폴더는 감시 폴더와 겹치지 않게 두는 것을 전제한다.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from soriham_api.ingest import parse_recorded_at

CHUNK = 1024 * 1024
STAGING_DIRNAME = ".incoming"
# 파일시스템 이름 길이 한계에 여유를 둔 상한. 문자가 아니라 바이트로 센다 —
# APFS는 255자지만 ext4는 255바이트라, 한글 200자는 리눅스에서 저장에 실패한다
MAX_NAME_BYTES = 200
# 이보다 오래된 스테이징 파일만 기동 시 정리한다 (다른 프로세스의 진행 중 업로드 보호)
STALE_AGE_SEC = 3600.0


class UploadTooLarge(Exception):
    """업로드가 설정된 크기 상한을 넘었다."""


class UploadEmpty(Exception):
    """빈 파일이 올라왔다."""


def safe_filename(raw: str | None) -> str | None:
    """업로드 파일명에서 경로 성분을 제거한다. 쓸 수 없는 이름이면 None."""
    if not raw:
        return None
    if "\x00" in raw:  # 널바이트는 파일시스템 호출에서 ValueError가 된다
        return None
    # 윈도우 클라이언트가 역슬래시 경로를 보내는 경우까지 잘라낸다
    name = Path(raw.replace("\\", "/")).name.strip()
    if not name or name in (".", ".."):
        return None
    return _shorten(name)


def stage_upload(src: BinaryIO, upload_dir: Path, max_bytes: int) -> Path:
    """업로드 스트림을 스테이징 폴더에 받아둔다. 실패하면 부분 파일을 남기지 않는다.

    확장자를 `.part`로 두는 이유: 저장 폴더가 실수로 감시 대상에 들어가도 미완성
    파일이 인제스트에 집히지 않게 한다.
    """
    staging = upload_dir / STAGING_DIRNAME
    staging.mkdir(parents=True, exist_ok=True)
    dest = staging / f"{uuid.uuid4().hex}.part"
    written = 0
    try:
        with dest.open("wb") as out:
            while chunk := src.read(CHUNK):
                written += len(chunk)
                if written > max_bytes:
                    raise UploadTooLarge
                out.write(chunk)
        if written == 0:
            raise UploadEmpty
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return dest


def finalize(
    staged: Path,
    upload_dir: Path,
    filename: str,
    taken: Callable[[Path], bool] | None = None,
) -> Path:
    """스테이징 파일을 `<upload_dir>/<YYYY-MM>/<파일명>`으로 옮긴다.

    `taken`은 디스크에 없더라도 쓰면 안 되는 경로를 걸러낸다(이미 DB에 등록된 경로).
    이걸 보지 않으면 파일만 지워진 기존 녹음의 경로를 새 파일이 덮어쓴다.
    """
    recorded = parse_recorded_at(filename) or datetime.now()
    target_dir = upload_dir / recorded.strftime("%Y-%m")
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_path(target_dir, filename, taken)
    # 같은 파일시스템이라 rename 한 번으로 원자적으로 자리를 잡는다
    os.replace(staged, dest)
    return dest


def cleanup_staging(upload_dir: Path, now: float | None = None) -> None:
    """이전 실행이 남긴 스테이징 잔여물을 지운다.

    실수로 두 번째 프로세스를 띄웠을 때 첫 프로세스가 받고 있는 파일을 지우지 않도록
    충분히 오래된 것만 건드린다.
    """
    staging = upload_dir / STAGING_DIRNAME
    if not staging.is_dir():
        return
    cutoff = (time.time() if now is None else now) - STALE_AGE_SEC
    for path in staging.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:  # 다른 프로세스가 먼저 치웠거나 접근 불가 — 다음 기동에 다시 시도
            continue


def _shorten(name: str) -> str:
    """파일시스템 이름 길이 한계를 넘지 않게 줄인다 (확장자는 보존)."""
    if len(name.encode()) <= MAX_NAME_BYTES:
        return name
    # 잘린 자리에 반쪽짜리 문자가 남지 않게 디코드에서 버린다
    suffix = Path(name).suffix.encode()[: MAX_NAME_BYTES // 2].decode(errors="ignore")
    budget = MAX_NAME_BYTES - len(suffix.encode())
    stem = Path(name).stem.encode()[:budget].decode(errors="ignore")
    return (stem or "audio") + suffix


def _unique_path(
    directory: Path, filename: str, taken: Callable[[Path], bool] | None = None
) -> Path:
    stem, suffix = Path(filename).stem, Path(filename).suffix
    unavailable = (lambda p: p.exists()) if taken is None else (lambda p: p.exists() or taken(p))
    candidate = directory / filename
    n = 2
    while unavailable(candidate):
        candidate = directory / f"{stem}-{n}{suffix}"
        n += 1
    return candidate
