"""업로드본이 디스크에서 어디 사는지.

경로 봉쇄 검사(허용된 뿌리 밖을 읽지 않는지)는 스트리밍 경로를 손볼 때 여기 붙는다.
"""

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
