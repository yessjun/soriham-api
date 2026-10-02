from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from watchdog.events import FileClosedEvent, FileCreatedEvent, FileModifiedEvent

from soriham_api import ingest, watch
from soriham_api.models import Recording


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(
        watch, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=lambda t: None)
    )
    return now


def test_same_size_modification_restarts_quiet_period(
    engine, workspace, tmp_path, monkeypatch, clock
):
    monkeypatch.setattr(ingest, "probe_duration", lambda path: 2.0)
    path = tmp_path / "sample.wav"
    path.write_bytes(b"first")
    handler = watch._Handler(sessionmaker(engine), workspace.id)
    handler.on_created(FileCreatedEvent(str(path)))
    clock[0] = 3.0
    path.write_bytes(b"other")
    handler.on_modified(FileModifiedEvent(str(path)))
    clock[0] = 5.0
    handler.process_ready()
    with sessionmaker(engine)() as db:
        assert db.scalar(select(func.count()).select_from(Recording)) == 0
    clock[0] = 8.0
    handler.process_ready()
    with sessionmaker(engine)() as db:
        assert db.scalars(select(Recording)).one().content_hash == ingest.content_hash(path)
    handler.on_closed(FileClosedEvent(str(path)))
    assert handler._pending == {}


def test_registration_failure_is_retried_and_does_not_block_other_file(
    engine, workspace, tmp_path, monkeypatch, clock
):
    monkeypatch.setattr(ingest, "probe_duration", lambda path: 2.0)
    original = watch.ingest_file
    failed = []

    def flaky(session, path, **kwargs):
        if path.name == "first.wav" and not failed:
            failed.append(path)
            raise RuntimeError("synthetic database failure")
        return original(session, path, **kwargs)

    monkeypatch.setattr(watch, "ingest_file", flaky)
    handler = watch._Handler(sessionmaker(engine), workspace.id)
    for name in ("first.wav", "second.wav"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        handler.on_created(FileCreatedEvent(str(path)))
    clock[0] = 5.0
    handler.process_ready()
    with sessionmaker(engine)() as db:
        assert db.scalars(select(Recording.filename)).all() == ["second.wav"]
    clock[0] = 10.0
    handler.process_ready()
    with sessionmaker(engine)() as db:
        assert len(db.scalars(select(Recording)).all()) == 2
    assert handler._pending == {}


def test_empty_file_does_not_block_ready_file(engine, workspace, tmp_path, monkeypatch, clock):
    monkeypatch.setattr(ingest, "probe_duration", lambda path: 2.0)
    handler = watch._Handler(sessionmaker(engine), workspace.id)
    empty = tmp_path / "empty.wav"
    full = tmp_path / "full.wav"
    empty.write_bytes(b"")
    full.write_bytes(b"full")
    for path in (empty, full):
        handler.on_created(FileCreatedEvent(str(path)))
    clock[0] = 5.0
    handler.process_ready()
    with sessionmaker(engine)() as db:
        assert db.scalars(select(Recording.filename)).all() == ["full.wav"]
    assert empty in handler._pending


@pytest.mark.parametrize("observer_alive", [False, True])
def test_dead_observer_or_emitter_ends_watch(monkeypatch, tmp_path, observer_alive):
    stopped = []
    observer = SimpleNamespace(
        schedule=lambda *a, **k: None,
        start=lambda: None,
        is_alive=lambda: observer_alive,
        emitters=[SimpleNamespace(is_alive=lambda: False)],
        stop=lambda: stopped.append(True),
        join=lambda **k: None,
    )
    monkeypatch.setattr(watch, "Observer", lambda: observer)
    with pytest.raises(RuntimeError, match="스레드"):
        watch.watch(None, (tmp_path,), workspace_id=1)
    assert stopped == [True]


def test_missing_emitter_ends_watch(monkeypatch, tmp_path):
    observer = SimpleNamespace(
        schedule=lambda *a, **k: None,
        start=lambda: None,
        is_alive=lambda: True,
        emitters=[],
        stop=lambda: None,
        join=lambda **k: None,
    )
    monkeypatch.setattr(watch, "Observer", lambda: observer)
    with pytest.raises(RuntimeError, match="스레드"):
        watch.watch(None, (tmp_path,), workspace_id=1)


def test_removed_watch_root_ends_watch(monkeypatch, tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    observer = SimpleNamespace(
        schedule=lambda *a, **k: None,
        start=root.rmdir,
        is_alive=lambda: True,
        emitters=[SimpleNamespace(is_alive=lambda: True)],
        stop=lambda: None,
        join=lambda **k: None,
    )
    monkeypatch.setattr(watch, "Observer", lambda: observer)
    with pytest.raises(RuntimeError, match="폴더"):
        watch.watch(None, (root,), workspace_id=1)


def test_native_watch_process_retries_and_indexes_modification(engine, workspace, tmp_path):
    import os
    import subprocess
    import sys
    import time

    root = tmp_path / "native"
    root.mkdir()
    code = """
import logging, os, sys, time
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from soriham_api import ingest, watch
logging.basicConfig(level=logging.INFO)
original_observer = watch.Observer
class ReadyObserver(original_observer):
    def start(self):
        super().start()
        print("READY", flush=True)
watch.Observer = ReadyObserver
watch.STABLE_QUIET_SEC = 0.1
watch.time = SimpleNamespace(monotonic=time.monotonic, sleep=lambda seconds: time.sleep(0.02))
ingest.probe_duration = lambda path: 2.0
original_ingest = watch.ingest_file
failed = []
def flaky(session, path, **kwargs):
    if path.name == "first.wav" and not failed:
        failed.append(path)
        raise RuntimeError("synthetic first registration failure")
    return original_ingest(session, path, **kwargs)
watch.ingest_file = flaky
watch.watch(sessionmaker(create_engine(os.environ["TEST_WATCH_DB"])),
            (Path(sys.argv[1]),), workspace_id=int(sys.argv[2]))
"""
    env = {**os.environ, "TEST_WATCH_DB": engine.url.render_as_string(hide_password=False)}
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code, str(root), str(workspace.id)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "READY"
        (root / "first.wav").write_bytes(b"first")
        (root / "second.wav").write_bytes(b"second")
        deadline = time.monotonic() + 15
        found = False
        while time.monotonic() < deadline:
            with sessionmaker(engine)() as db:
                count = db.scalar(select(func.count()).select_from(Recording))
                found = count == 2
            if found:
                break
            assert process.poll() is None
            time.sleep(0.05)
        assert found
        (root / "first.wav").write_bytes(b"other")
        deadline = time.monotonic() + 15
        found = False
        while time.monotonic() < deadline:
            with sessionmaker(engine)() as db:
                rows = db.scalars(select(Recording)).all()
                found = len(rows) == 3 and sum(row.path_current for row in rows) == 2
            if found:
                break
            assert process.poll() is None
            time.sleep(0.05)
        assert found
    finally:
        process.terminate()
        _, errors = process.communicate(timeout=5)
    assert "synthetic first registration failure" in errors
