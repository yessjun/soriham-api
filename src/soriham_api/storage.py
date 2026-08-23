"""업로드본이 디스크에서 어디 사는지, 그리고 무엇을 읽어도 되는지."""

from __future__ import annotations

import uuid
from pathlib import Path

# 워크스페이스별 하위 트리를 모아 두는 자리. 스테이징(.incoming)은 최상위에 그대로 둔다 —
# 확정 이동이 같은 파일시스템 rename 한 번으로 끝나야 하고 잔여물 청소가 한 곳만 보면 된다
WORKSPACE_DIRNAME = "w"


def workspace_upload_dir(upload_dir: Path, workspace_public_id: uuid.UUID) -> Path:
    """워크스페이스의 업로드 뿌리.

    슬러그가 아니라 public_id로 가르는 이유: 워크스페이스 이름을 바꿔도 이미 저장된
    파일이 움직이지 않는다.
    """
    return upload_dir / WORKSPACE_DIRNAME / workspace_public_id.hex


class AudioUnavailable(Exception):
    """내보낼 수 없는 오디오. 이유는 밖에 말하지 않는다."""


def resolve_audio_path(
    stored_path: str,
    *,
    source: str,
    workspace_public_id: uuid.UUID,
    upload_dir: Path | None,
    audio_dirs: tuple[Path, ...],
) -> Path:
    """행에 적힌 경로가 허용된 뿌리 안인지 확인하고 실제 경로를 돌려준다.

    행이 위조되거나 손상돼도 남의 파일을 못 읽게 하는 마지막 방벽이다. 업로드본은
    **그 녹음 자신의 워크스페이스 하위**여야 하고, 스캔본은 감시 폴더 하위여야 한다.

    심링크를 먼저 따라간다. 안 따라가면 허용된 뿌리 안에 링크 하나만 놓아도 밖이
    읽힌다.
    """
    real = Path(stored_path).resolve()
    if source == "upload":
        if upload_dir is None:
            raise AudioUnavailable("업로드 폴더가 설정돼 있지 않습니다")
        roots = [workspace_upload_dir(upload_dir, workspace_public_id).resolve()]
    else:
        roots = [d.expanduser().resolve() for d in audio_dirs]
    if not roots or not any(real == r or real.is_relative_to(r) for r in roots):
        raise AudioUnavailable("허용된 폴더 밖의 경로입니다")
    if not real.is_file():
        raise AudioUnavailable("파일이 없습니다")
    return real
