"""감시 폴더: 새 녹음 파일이 생기면 크기가 안정된 뒤 등록한다."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from soriham_api.ingest import AUDIO_EXTENSIONS, ingest_file

logger = logging.getLogger(__name__)

# 복사 중인 파일을 붙잡지 않도록 크기가 이 간격 동안 그대로면 완료로 본다
STABLE_INTERVAL_SEC = 2.0
STABLE_TIMEOUT_SEC = 600.0


def wait_until_stable(path: Path) -> bool:
    deadline = time.monotonic() + STABLE_TIMEOUT_SEC
    last = -1
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size == last and size > 0:
            return True
        last = size
        time.sleep(STABLE_INTERVAL_SEC)
    return False


class _Handler(FileSystemEventHandler):
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_ingest(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._maybe_ingest(event)

    def _maybe_ingest(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(str(getattr(event, "dest_path", "") or event.src_path))
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            return
        if not wait_until_stable(path):
            logger.warning("파일 크기가 안정되지 않아 건너뜀: %s", path)
            return
        with self._session_factory() as session:
            created = ingest_file(session, path)
            session.commit()
        if created is not None:
            logger.info("감시 등록: %s (%s)", path.name, created.status)


def watch(session_factory: sessionmaker[Session], dirs: tuple[Path, ...]) -> None:
    """감시를 시작하고 중단(Ctrl-C)까지 블로킹한다."""
    observer = Observer()
    handler = _Handler(session_factory)
    for d in dirs:
        if d.is_dir():
            observer.schedule(handler, str(d), recursive=True)
            logger.info("감시 시작: %s", d)
        else:
            logger.warning("감시 폴더가 없습니다: %s", d)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
