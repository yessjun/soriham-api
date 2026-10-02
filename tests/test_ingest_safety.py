from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from conftest import login, make_settings
from soriham_api import ingest, worker
from soriham_api.app import create_app
from soriham_api.models import JobLog, Recording, ShareLink, SpeakerName
from soriham_api.quota import measure
from soriham_api.stt_client import RunnerUnavailable
from test_worker import FakeRunnerClient


@pytest.fixture(autouse=True)
def probe(monkeypatch):
    monkeypatch.setattr(ingest, "probe_duration", lambda path: 2.0)


def register(db, tmp_path, workspace, content=b"old audio"):
    path = tmp_path / "rec" / "sample.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    row, _ = ingest.ingest_file(db, path, workspace_id=workspace.id)
    db.commit()
    return path, row


def test_replacement_preserves_old_record_and_blocks_old_audio(
    engine, db, owner, workspace, tmp_path
):
    path, old = register(db, tmp_path, workspace)
    worker.process_one(db, FakeRunnerClient())
    old.title = "preserved title"
    old.summary = "preserved summary"
    db.add(
        SpeakerName(recording_id=old.id, speaker_key="SPEAKER_00", display_name="synthetic name")
    )
    db.commit()
    app = create_app(
        settings=make_settings(audio_dirs=(path.parent,)), session_factory=sessionmaker(engine)
    )
    client = TestClient(app)
    login(client, owner.email)
    issued = client.post(f"/api/recordings/{old.public_id}/links", json={"allow_audio": True})
    assert issued.status_code == 201
    token = issued.json()["token"]
    guest = TestClient(app)
    assert guest.get(f"/api/shared/{token}/audio").status_code == 200

    path.write_bytes(b"new audio")
    assert client.get(f"/api/recordings/{old.public_id}/audio").status_code == 404
    assert guest.get(f"/api/shared/{token}/audio").status_code == 404
    new, outcome = ingest.ingest_file(db, path, workspace_id=workspace.id)
    db.commit()
    db.refresh(old)
    assert outcome == "new" and new.public_id != old.public_id
    assert old.status == "missing" and not old.path_current
    assert old.summary == "preserved summary" and old.title == "preserved title"
    assert len(old.segments) == 2 and len(old.speaker_names) == 1
    assert new.summary is None and new.segments == [] and new.speaker_names == []
    assert db.scalars(select(ShareLink)).one().recording_id == old.id
    assert client.get(f"/api/recordings/{old.public_id}/audio").status_code == 404
    assert guest.get(f"/api/shared/{token}/audio").status_code == 404
    assert client.get(f"/api/recordings/{new.public_id}/audio").content == b"new audio"
    assert measure(db, workspace).used_bytes == len(b"new audio")


def test_original_content_restores_original_identity(db, tmp_path, workspace):
    path, old = register(db, tmp_path, workspace)
    old.summary = "preserved"
    db.commit()
    path.write_bytes(b"new audio")
    replacement, _ = ingest.ingest_file(db, path, workspace_id=workspace.id)
    db.commit()
    path.write_bytes(b"old audio")
    restored, outcome = ingest.ingest_file(db, path, workspace_id=workspace.id)
    db.commit()
    assert outcome == "moved" and restored.id == old.id
    assert restored.status == "done" and restored.summary == "preserved"
    assert restored.path_current and not replacement.path_current


def test_unchanged_file_uses_validated_signature(db, tmp_path, workspace, monkeypatch):
    path, old = register(db, tmp_path, workspace)
    monkeypatch.setattr(ingest, "content_hash", lambda path: pytest.fail("unexpected full read"))
    row, outcome = ingest.ingest_file(db, path, workspace_id=workspace.id)
    assert row.id == old.id and outcome == "existing"


def test_reading_changed_file_does_not_register(db, tmp_path, workspace, monkeypatch):
    path = tmp_path / "sample.wav"
    path.write_bytes(b"old audio")
    original = ingest.content_hash

    def changing_hash(path):
        digest = original(path)
        path.write_bytes(b"new audio")
        return digest

    monkeypatch.setattr(ingest, "content_hash", changing_hash)
    with pytest.raises(ingest.FileChangedDuringRead):
        ingest.ingest_file(db, path, workspace_id=workspace.id)
    assert db.scalar(select(func.count()).select_from(Recording)) == 0


def test_reappearance_cannot_change_other_workspace(db, tmp_path, workspace, other_workspace):
    path, old = register(db, tmp_path, workspace)
    old.status = "missing"
    db.commit()
    with pytest.raises(ValueError):
        ingest.ingest_file(db, path, workspace_id=other_workspace.id)
    assert old.status == "missing" and old.workspace_id == workspace.id


def test_scan_cannot_change_uploaded_record(db, tmp_path, workspace):
    path = tmp_path / "sample.wav"
    path.write_bytes(b"old audio")
    old, _ = ingest.ingest_file(db, path, workspace_id=workspace.id, source="upload")
    db.commit()
    with pytest.raises(ValueError):
        ingest.ingest_file(db, path, workspace_id=workspace.id)
    assert old.path_current and old.source == "upload"


def test_blackout_guard_is_per_root(db, tmp_path, workspace):
    roots = (tmp_path / "a", tmp_path / "b")
    for root in roots:
        root.mkdir()
    for i in range(ingest.SWEEP_BLACKOUT_MIN + 1):
        (roots[0] / f"{i}.wav").write_bytes(str(i).encode())
    (roots[1] / "present.wav").write_bytes(b"present")
    ingest.scan(db, roots, workspace_id=workspace.id)
    for path in roots[0].iterdir():
        path.unlink()
    assert ingest.scan(db, roots, workspace_id=workspace.id)["missing"] == 0
    assert db.scalar(select(func.count()).where(Recording.status == "missing")) == 0


def test_missing_device_is_not_file_deletion(db, tmp_path, workspace):
    path, old = register(db, tmp_path, workspace)
    signature = list(old.file_signature)
    signature[0] += 1
    old.file_signature = signature
    db.commit()
    path.unlink()
    assert ingest.scan(db, (path.parent,), workspace_id=workspace.id)["missing"] == 0
    db.refresh(old)
    assert old.status == "pending"


def test_failed_traversal_does_not_mark_missing(db, tmp_path, workspace, monkeypatch):
    path, old = register(db, tmp_path, workspace)
    path.unlink()

    def denied(*args, **kwargs):
        raise PermissionError("synthetic traversal failure")

    monkeypatch.setattr(ingest.os, "walk", denied)
    assert ingest.scan(db, (path.parent,), workspace_id=workspace.id)["missing"] == 0
    db.refresh(old)
    assert old.status == "pending"


@pytest.mark.parametrize("unavailable", [False, True])
def test_late_worker_result_cannot_replace_current_record(
    engine, db, tmp_path, workspace, unavailable
):
    path, old = register(db, tmp_path, workspace)
    new_id = []

    class ReplacingRunner(FakeRunnerClient):
        def transcribe(self, *args, **kwargs):
            path.write_bytes(b"new audio")
            with Session(engine) as other:
                new, _ = ingest.ingest_file(other, path, workspace_id=workspace.id)
                other.commit()
                new_id.append(new.id)
            if unavailable:
                raise RunnerUnavailable("synthetic connection loss")
            return super().transcribe(*args, **kwargs)

    assert worker.process_one(db, ReplacingRunner())
    db.refresh(old)
    new = db.get(Recording, new_id[0])
    assert old.status == "missing" and old.segments == []
    assert new.status == "pending" and new.segments == []
    assert not db.scalars(select(JobLog).where(JobLog.status == "done")).all()
    assert worker.process_one(db, FakeRunnerClient())
    db.refresh(new)
    assert new.status == "done" and len(new.segments) == 2


def test_archived_record_is_not_claimable(db, tmp_path, workspace):
    path, old = register(db, tmp_path, workspace)
    path.write_bytes(b"new audio")
    new, _ = ingest.ingest_file(db, path, workspace_id=workspace.id)
    old.status = "error"
    db.commit()
    assert worker.retry_failed(db) == 0
    claimed = worker.claim_next(db)
    assert claimed.id == new.id


def test_source_change_without_scan_does_not_store_transcript(db, tmp_path, workspace):
    path, old = register(db, tmp_path, workspace)

    class WritingRunner(FakeRunnerClient):
        def transcribe(self, *args, **kwargs):
            path.write_bytes(b"changed while running")
            return super().transcribe(*args, **kwargs)

    assert worker.process_one(db, WritingRunner())
    db.refresh(old)
    assert old.status == "error" and old.segments == []
    assert old.error.startswith(ingest.SOURCE_CHANGED_PREFIX)
    assert not db.scalars(select(JobLog).where(JobLog.status == "done")).all()
    assert ingest.scan(db, (path.parent,), workspace_id=workspace.id)["new"] == 1


def test_source_change_before_submit_does_not_call_runner(db, tmp_path, workspace):
    path, old = register(db, tmp_path, workspace)
    path.write_bytes(b"changed before submit")
    runner = FakeRunnerClient()
    assert worker.process_one(db, runner)
    db.refresh(old)
    assert old.status == "error" and runner.calls == []


def test_current_path_uniqueness_is_enforced(db, tmp_path, workspace):
    from sqlalchemy.exc import IntegrityError

    path, old = register(db, tmp_path, workspace)
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(
            Recording(
                workspace_id=workspace.id,
                path=str(path),
                filename="other.wav",
                size_bytes=1,
                partial_hash="synthetic",
                path_current=True,
            )
        )
        db.flush()
    assert old.path_current


def test_downgrade_refuses_to_delete_replacement_history(engine, db, tmp_path, workspace):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    path, old = register(db, tmp_path, workspace)
    old.summary = "preserved"
    db.commit()
    path.write_bytes(b"new audio")
    ingest.ingest_file(db, path, workspace_id=workspace.id)
    db.commit()
    cfg = Config("alembic.ini")
    cfg.set_main_option(
        "sqlalchemy.url", engine.url.render_as_string(hide_password=False).replace("%", "%%")
    )
    with pytest.raises(RuntimeError, match="과거 녹음"):
        command.downgrade(cfg, "356e1ddfca3c")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "ed92879e0eb9"
        assert connection.scalar(select(func.count()).select_from(Recording)) == 2
        assert (
            connection.scalar(select(Recording.summary).where(Recording.id == old.id))
            == "preserved"
        )
