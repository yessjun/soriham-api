"""환경 변수 기반 설정. 시크릿은 머신별 .env로만 관리한다."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """api 프로세스 전역 설정.

    - DATABASE_URL: postgres 접속 문자열 (예: postgresql+psycopg://user:pw@host/db)
    - AUDIO_DIRS: 스캔·감시할 녹음 폴더들 (경로 나열, 구분자 os.pathsep)
    - RUNNER_URL: stt 러너 주소 (로컬 프로세스든 원격 pod든 같은 계약)
    - RUNNER_UPLOAD: true면 오디오를 업로드, 아니면 경로 전달(파일시스템 공유 전제)
    - STT_MODEL / STT_LANGUAGE: 러너에 넘길 모델·언어 (비우면 러너 기본값·자동 감지)
    """

    database_url: str
    audio_dirs: tuple[Path, ...]
    runner_url: str
    runner_upload: bool
    stt_model: str | None
    stt_language: str | None


def load_settings(env: dict[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    _load_dotenv(e)
    url = e.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL이 설정돼 있지 않습니다 (.env 확인)")
    dirs = tuple(Path(p).expanduser() for p in (e.get("AUDIO_DIRS") or "").split(os.pathsep) if p)
    return Settings(
        database_url=url,
        audio_dirs=dirs,
        runner_url=e.get("RUNNER_URL") or "http://localhost:8100",
        runner_upload=(e.get("RUNNER_UPLOAD") or "").lower() in ("1", "true", "yes"),
        stt_model=e.get("STT_MODEL") or None,
        stt_language=e.get("STT_LANGUAGE") or None,
    )


def _load_dotenv(e: dict[str, str]) -> None:
    """레포 루트의 .env를 읽어 미설정 키만 채운다 (의존성 없는 최소 구현)."""
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in e:
            e[key] = value.strip().strip("'\"")
