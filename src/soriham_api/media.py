"""오디오 바이트를 내보내는 응답.

Range 처리를 라우트에서 떼어낸다 — 로그인 재생과 링크 재생이 같은 코드를 써야
한쪽만 고쳐지는 일이 없다.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from fastapi.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from soriham_api.files import stat_signature, stream_hash

CHUNK_SIZE = 1024 * 256

MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wma": "audio/x-ms-wma",
    ".amr": "audio/amr",
}


def media_type(path: Path) -> str:
    return MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def range_response(
    path: Path,
    range_header: str | None,
    *,
    headers: dict[str, str] | None = None,
    expected_hash: str | None = None,
    expected_signature: list[int] | None = None,
    verify_identity: bool = False,
) -> Response:
    extra = headers or {}
    try:
        source = path.open("rb")
        signature = stat_signature(os.fstat(source.fileno()))
        if verify_identity and expected_signature != list(signature):
            if expected_hash is None or stream_hash(source) != expected_hash:
                source.close()
                return Response(status_code=404, headers=extra)
            if stat_signature(os.fstat(source.fileno())) != signature:
                source.close()
                return Response(status_code=404, headers=extra)
            source.seek(0)
    except OSError:
        if "source" in locals():
            source.close()
        return Response(status_code=404, headers=extra)
    size = signature[2]
    if not range_header:
        return _AudioResponse(
            source,
            _iter_stream(source, 0, size - 1, signature, expected_hash),
            media_type=media_type(path),
            headers={"accept-ranges": "bytes", "content-length": str(size), **extra},
        )
    try:
        unit, _, spec = range_header.partition("=")
        start_s, _, end_s = spec.strip().partition("-")
        if unit.strip().lower() != "bytes":
            raise ValueError
        if start_s:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        else:
            # suffix range: 마지막 N바이트
            start = max(0, size - int(end_s))
            end = size - 1
        if start > end or start >= size:
            raise ValueError
    except ValueError:
        source.close()
        return Response(status_code=416, headers={"content-range": f"bytes */{size}", **extra})
    end = min(end, size - 1)
    return _AudioResponse(
        source,
        _iter_stream(source, start, end, signature, expected_hash),
        status_code=206,
        media_type=media_type(path),
        headers={
            "accept-ranges": "bytes",
            "content-range": f"bytes {start}-{end}/{size}",
            "content-length": str(end - start + 1),
            **extra,
        },
    )


def iter_file(path: Path, start: int, end: int) -> Iterator[bytes]:
    with path.open("rb") as f:
        yield from _iter_stream(f, start, end, stat_signature(os.fstat(f.fileno())), None)


def _iter_stream(
    source: BinaryIO, start: int, end: int, signature, expected_hash
) -> Iterator[bytes]:
    source.seek(start)
    remaining = end - start + 1
    while remaining > 0:
        chunk = source.read(min(CHUNK_SIZE, remaining))
        if not chunk:
            break
        now = stat_signature(os.fstat(source.fileno()))
        if now != signature:
            if expected_hash is None:
                break
            position = source.tell()
            source.seek(0)
            digest = stream_hash(source)
            after = stat_signature(os.fstat(source.fileno()))
            source.seek(position)
            if digest != expected_hash or after != now:
                break
            signature = after
        remaining -= len(chunk)
        yield chunk


class _AudioResponse(StreamingResponse):
    def __init__(self, source: BinaryIO, content: Iterator[bytes], **kwargs) -> None:
        super().__init__(content, **kwargs)
        self._source = source

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._source.close()
