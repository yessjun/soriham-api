from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from soriham_api.storage import AudioUnavailable, resolve_audio_path, workspace_upload_dir

WS = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_WS = uuid.UUID("22222222-2222-2222-2222-222222222222")


def write(path: Path, content: bytes = b"RIFF") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_업로드본은_자기_워크스페이스_하위여야_한다(tmp_path: Path):
    up = tmp_path / "uploads"
    mine = write(workspace_upload_dir(up, WS) / "2026-08" / "a.wav")

    assert (
        resolve_audio_path(
            str(mine), source="upload", workspace_public_id=WS, upload_dir=up, audio_dirs=()
        )
        == mine.resolve()
    )


def test_남의_워크스페이스_하위는_읽지_않는다(tmp_path: Path):
    """행이 위조되거나 손상돼도 남의 파일에 닿지 않게 하는 마지막 검사다."""
    up = tmp_path / "uploads"
    theirs = write(workspace_upload_dir(up, OTHER_WS) / "2026-08" / "b.wav")

    with pytest.raises(AudioUnavailable):
        resolve_audio_path(
            str(theirs), source="upload", workspace_public_id=WS, upload_dir=up, audio_dirs=()
        )


def test_업로드_폴더_밖은_읽지_않는다(tmp_path: Path):
    up = tmp_path / "uploads"
    outside = write(tmp_path / "etc" / "secret.wav")

    with pytest.raises(AudioUnavailable):
        resolve_audio_path(
            str(outside), source="upload", workspace_public_id=WS, upload_dir=up, audio_dirs=()
        )


def test_심링크로_뿌리_밖을_가리켜도_막는다(tmp_path: Path):
    """심링크를 안 따라가면 허용된 뿌리 안에 링크 하나만 놓아도 밖이 읽힌다."""
    up = tmp_path / "uploads"
    secret = write(tmp_path / "etc" / "secret.wav")
    inside = workspace_upload_dir(up, WS) / "2026-08"
    inside.mkdir(parents=True, exist_ok=True)
    link = inside / "looks-fine.wav"
    link.symlink_to(secret)

    with pytest.raises(AudioUnavailable):
        resolve_audio_path(
            str(link), source="upload", workspace_public_id=WS, upload_dir=up, audio_dirs=()
        )


def test_상대경로_탈출도_막는다(tmp_path: Path):
    up = tmp_path / "uploads"
    write(tmp_path / "etc" / "secret.wav")
    # 중간 폴더를 실제로 만들어 둔다. 없으면 경로 검사가 아니라 파일 부재로 막혀서
    # 검사를 지워도 테스트가 통과한다
    workspace_upload_dir(up, WS).mkdir(parents=True, exist_ok=True)
    escape = workspace_upload_dir(up, WS) / ".." / ".." / ".." / "etc" / "secret.wav"

    with pytest.raises(AudioUnavailable):
        resolve_audio_path(
            str(escape), source="upload", workspace_public_id=WS, upload_dir=up, audio_dirs=()
        )


def test_스캔본은_감시_폴더_하위여야_한다(tmp_path: Path):
    audio = tmp_path / "audio"
    inside = write(audio / "2026" / "a.wav")
    outside = write(tmp_path / "elsewhere" / "b.wav")

    assert (
        resolve_audio_path(
            str(inside), source="scan", workspace_public_id=WS, upload_dir=None, audio_dirs=(audio,)
        )
        == inside.resolve()
    )
    with pytest.raises(AudioUnavailable):
        resolve_audio_path(
            str(outside),
            source="scan",
            workspace_public_id=WS,
            upload_dir=None,
            audio_dirs=(audio,),
        )


def test_감시_폴더가_설정에_없으면_스캔본을_못_읽는다(tmp_path: Path):
    """serve 프로세스가 AUDIO_DIRS 없이 뜨면 스캔 녹음은 재생되지 않는다.

    조용히 아무 경로나 열어주는 것보다 낫다. 설정이 빠졌다는 것은 화면에서 드러난다.
    """
    inside = write(tmp_path / "audio" / "a.wav")
    with pytest.raises(AudioUnavailable):
        resolve_audio_path(
            str(inside), source="scan", workspace_public_id=WS, upload_dir=None, audio_dirs=()
        )


def test_업로드_폴더가_없으면_업로드본을_못_읽는다(tmp_path: Path):
    path = write(tmp_path / "a.wav")
    with pytest.raises(AudioUnavailable):
        resolve_audio_path(
            str(path), source="upload", workspace_public_id=WS, upload_dir=None, audio_dirs=()
        )


def test_파일이_없으면_뿌리_안이어도_못_읽는다(tmp_path: Path):
    up = tmp_path / "uploads"
    gone = workspace_upload_dir(up, WS) / "2026-08" / "gone.wav"
    gone.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(AudioUnavailable):
        resolve_audio_path(
            str(gone), source="upload", workspace_public_id=WS, upload_dir=up, audio_dirs=()
        )


def test_감시_폴더가_심링크여도_그_아래는_읽는다(tmp_path: Path):
    """뿌리도 실경로로 편 뒤에 비교해야 한다.

    macOS의 /tmp는 /private/tmp의 심링크이고 NAS 마운트도 흔히 링크다. 한쪽만 펴면
    허용해야 할 파일을 전부 막는다 — 막는 쪽 실수라 조용하지만, 재생이 통째로 죽는다.
    """
    real_dir = tmp_path / "audio"
    inside = write(real_dir / "a.wav")
    alias = tmp_path / "alias"
    alias.symlink_to(real_dir)

    assert (
        resolve_audio_path(
            str(inside), source="scan", workspace_public_id=WS, upload_dir=None, audio_dirs=(alias,)
        )
        == inside.resolve()
    )


def test_심링크_경로로_들어와도_같은_파일로_본다(tmp_path: Path):
    """행에 적힌 경로가 링크를 거쳐도, 뿌리 안의 같은 파일이면 허용해야 한다."""
    real_dir = tmp_path / "audio"
    inside = write(real_dir / "a.wav")
    alias = tmp_path / "alias"
    alias.symlink_to(real_dir)

    assert (
        resolve_audio_path(
            str(alias / "a.wav"),
            source="scan",
            workspace_public_id=WS,
            upload_dir=None,
            audio_dirs=(real_dir,),
        )
        == inside.resolve()
    )
