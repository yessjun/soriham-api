"""현재 파일을 검증한 바이트와 같은 파일인지 확인하는 읽기 도구."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import BinaryIO


def stat_signature(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def file_signature(path: Path) -> tuple[int, int, int, int, int]:
    return stat_signature(path.stat())


def stream_hash(source: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := source.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def content_hash(path: Path) -> str:
    with path.open("rb") as source:
        return stream_hash(source)
