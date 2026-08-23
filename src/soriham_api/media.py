"""오디오 바이트를 내보내는 응답.

Range 처리를 라우트에서 떼어낸다 — 로그인 재생과 링크 재생이 같은 코드를 써야
한쪽만 고쳐지는 일이 없다.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi.responses import Response, StreamingResponse

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
    path: Path, range_header: str | None, *, headers: dict[str, str] | None = None
) -> Response:
    size = path.stat().st_size
    extra = headers or {}
    if not range_header:
        return StreamingResponse(
            iter_file(path, 0, size - 1),
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
        return Response(status_code=416, headers={"content-range": f"bytes */{size}", **extra})
    end = min(end, size - 1)
    return StreamingResponse(
        iter_file(path, start, end),
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
    remaining = end - start + 1
    with path.open("rb") as f:
        f.seek(start)
        while remaining > 0:
            chunk = f.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
