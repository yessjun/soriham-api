"""파일 이벤트를 모아 쓰기가 잠잠해진 파일부터 등록한다."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from soriham_api.ingest import AUDIO_EXTENSIONS, file_signature, ingest_file

logger = logging.getLogger(__name__)
STABLE_QUIET_SEC = 5.0


class _Handler(FileSystemEventHandler):
    def __init__(self, session_factory: sessionmaker[Session], workspace_id: int) -> None:
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self._lock = threading.Lock()
        self._pending: dict[Path, tuple[tuple[int, ...], float]] = {}
        self._registered: dict[Path, tuple[int, ...]] = {}

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_ingest(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._maybe_ingest(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        # 같은 크기·시각으로 관측돼도 수정 이벤트는 다시 검증한다
        if not event.is_directory:
            path = Path(str(event.src_path))
            with self._lock:
                self._registered.pop(path, None)
                self._pending.pop(path, None)
        self._maybe_ingest(event)

    def on_closed(self, event: FileSystemEvent) -> None:
        self._maybe_ingest(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        path = Path(str(event.src_path))
        with self._lock:
            self._pending.pop(path, None)
            self._registered.pop(path, None)

    def _maybe_ingest(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(str(getattr(event, "dest_path", "") or event.src_path))
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            return
        try:
            signature = file_signature(path)
        except OSError:
            logger.warning("감시 파일의 상태를 읽지 못함: %s", path.name)
            return
        with self._lock:
            if self._registered.get(path) == signature:
                return
            old = self._pending.get(path)
            if old is None or old[0] != signature:
                self._pending[path] = (signature, time.monotonic() + STABLE_QUIET_SEC)

    def process_ready(self) -> None:
        now = time.monotonic()
        with self._lock:
            ready = [(path, item) for path, item in self._pending.items() if item[1] <= now]
        for path, item in ready:
            try:
                signature = file_signature(path)
                if signature != item[0] or signature[2] == 0:
                    self._retry(path, item, signature)
                    continue
                with self._session_factory() as session:
                    created, outcome = ingest_file(
                        session, path, workspace_id=self._workspace_id, verify_existing=True
                    )
                    session.commit()
                    saved_signature = created.file_signature if created is not None else None
                    saved_status = created.status if created is not None else None
                with self._lock:
                    # 해시를 읽는 동안 들어온 더 새로운 이벤트는 남겨 둔다
                    if self._pending.get(path) is item:
                        del self._pending[path]
                    if saved_signature == list(signature):
                        self._registered[path] = signature
                if created is not None and outcome != "existing":
                    logger.info("감시 등록: %s (%s, %s)", path.name, outcome, saved_status)
            except FileNotFoundError:
                with self._lock:
                    if self._pending.get(path) is item:
                        del self._pending[path]
            except Exception:  # noqa: BLE001 - 등록 실패가 감시와 다른 파일 처리를 끊지 않게 한다
                logger.exception("감시 등록 실패: %s", path.name)
                self._retry(path, item, item[0])

    def _retry(self, path: Path, item, signature: tuple[int, ...]) -> None:
        with self._lock:
            if self._pending.get(path) is item:
                self._pending[path] = (signature, time.monotonic() + STABLE_QUIET_SEC)


def watch(
    session_factory: sessionmaker[Session], dirs: tuple[Path, ...], *, workspace_id: int
) -> None:
    observer = Observer()
    handler = _Handler(session_factory, workspace_id)
    roots = sorted({directory.resolve() for directory in dirs})
    identities = {}
    for directory in roots:
        if directory.is_dir():
            stat = directory.stat()
            identities[directory] = (stat.st_dev, stat.st_ino)
            observer.schedule(handler, str(directory), recursive=True)
            logger.info("감시 시작: %s", directory)
        else:
            logger.warning("감시 폴더가 없습니다: %s", directory)
    if not identities:
        raise RuntimeError("감시할 수 있는 폴더가 없습니다")
    observer.start()
    try:
        while True:
            emitters = tuple(observer.emitters)
            if (
                not observer.is_alive()
                or len(emitters) < len(identities)
                or not all(emitter.is_alive() for emitter in emitters)
            ):
                raise RuntimeError("파일 감시 스레드가 중단됐습니다")
            for directory, identity in identities.items():
                try:
                    stat = directory.stat()
                except OSError as exc:
                    raise RuntimeError("감시 폴더가 사라졌습니다") from exc
                if identity != (stat.st_dev, stat.st_ino):
                    raise RuntimeError("감시 폴더의 장치가 바뀌었습니다")
            handler.process_ready()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join(timeout=5)
