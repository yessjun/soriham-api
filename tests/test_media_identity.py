from __future__ import annotations

import asyncio

from soriham_api.files import content_hash, file_signature
from soriham_api.media import range_response


def collect(response):
    async def body():
        return b"".join([chunk async for chunk in response.body_iterator])

    try:
        return asyncio.run(body())
    finally:
        response._source.close()


def test_atomic_replacement_does_not_change_open_audio(tmp_path):
    path = tmp_path / "sample.wav"
    path.write_bytes(b"old audio")
    response = range_response(
        path,
        None,
        expected_hash=content_hash(path),
        expected_signature=list(file_signature(path)),
        verify_identity=True,
    )
    replacement = tmp_path / "replacement.wav"
    replacement.write_bytes(b"new audio")
    replacement.replace(path)
    assert collect(response) == b"old audio"


def test_in_place_write_does_not_send_new_audio(tmp_path):
    path = tmp_path / "sample.wav"
    path.write_bytes(b"old audio")
    response = range_response(
        path,
        None,
        expected_hash=content_hash(path),
        expected_signature=list(file_signature(path)),
        verify_identity=True,
    )
    path.write_bytes(b"new audio")
    assert collect(response) == b""


def test_audio_without_identity_is_not_shared(tmp_path):
    path = tmp_path / "sample.wav"
    path.write_bytes(b"unknown audio")
    assert range_response(path, None, verify_identity=True).status_code == 404
