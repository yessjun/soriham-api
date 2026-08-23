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
    - CORS_ORIGINS: 콘솔 오리진 목록(콤마 구분) — 브라우저 경유 접근을 콘솔로 한정
    - UPLOAD_DIR: 콘솔 업로드본을 저장할 폴더 (비우면 업로드 기능 비활성).
      **AUDIO_DIRS와 겹치면 기동을 거부한다** — 겹치면 남의 업로드가 스캔에 집혀
      엉뚱한 워크스페이스로 흘러든다
    - DEFAULT_WORKSPACE: 스캔·감시·업로드가 녹음을 넣을 워크스페이스 슬러그
    - MAX_UPLOAD_MB: 업로드 한 건의 크기 상한 (기본 4096)
    - ENRICH_BACKEND: 제목/요약/태그 생성 백엔드 (ollama | claude | off)
    - OLLAMA_URL / OLLAMA_MODEL: ollama 백엔드 설정
    """

    database_url: str
    audio_dirs: tuple[Path, ...]
    runner_url: str
    runner_upload: bool
    stt_model: str | None
    stt_language: str | None
    cors_origins: tuple[str, ...]
    upload_dir: Path | None
    default_workspace: str | None
    max_upload_bytes: int
    enrich_backend: str
    ollama_url: str
    ollama_model: str


def load_settings(env: dict[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    _load_dotenv(e)
    url = e.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL이 설정돼 있지 않습니다 (.env 확인)")
    dirs = tuple(Path(p).expanduser() for p in (e.get("AUDIO_DIRS") or "").split(os.pathsep) if p)
    upload_dir = Path(e["UPLOAD_DIR"]).expanduser() if e.get("UPLOAD_DIR") else None
    _reject_overlap(dirs, upload_dir)
    return Settings(
        database_url=url,
        audio_dirs=dirs,
        runner_url=e.get("RUNNER_URL") or "http://localhost:8100",
        runner_upload=(e.get("RUNNER_UPLOAD") or "").lower() in ("1", "true", "yes"),
        stt_model=e.get("STT_MODEL") or None,
        stt_language=e.get("STT_LANGUAGE") or None,
        cors_origins=tuple(
            o.strip()
            for o in (e.get("CORS_ORIGINS") or "http://localhost:5174").split(",")
            if o.strip()
        ),
        upload_dir=upload_dir,
        default_workspace=e.get("DEFAULT_WORKSPACE") or None,
        max_upload_bytes=int(e.get("MAX_UPLOAD_MB") or 4096) * 1024 * 1024,
        enrich_backend=e.get("ENRICH_BACKEND") or "ollama",
        ollama_url=e.get("OLLAMA_URL") or "http://localhost:11434",
        ollama_model=e.get("OLLAMA_MODEL") or "qwen3:8b",
    )


def _reject_overlap(audio_dirs: tuple[Path, ...], upload_dir: Path | None) -> None:
    """감시 폴더와 업로드 폴더가 겹치면 기동을 거부한다.

    겹치면 한 사람의 업로드가 스캔에 집혀 다른 워크스페이스의 녹음으로 등록된다.
    오디오 경로 봉쇄 검사는 이걸 못 잡는다 — 파일이 실제로 감시 폴더 아래 있어서
    검사를 통과한다. 운영자가 안 겹치게 두는 것에 기대지 않는다.
    """
    if upload_dir is None:
        return
    up = upload_dir.expanduser().resolve()
    for d in audio_dirs:
        base = d.expanduser().resolve()
        if up == base or up.is_relative_to(base) or base.is_relative_to(up):
            raise RuntimeError(
                f"UPLOAD_DIR({upload_dir})와 AUDIO_DIRS({d})가 겹칩니다. "
                "겹치면 업로드본이 스캔에 집혀 다른 워크스페이스로 등록됩니다"
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
