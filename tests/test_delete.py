"""녹음 삭제.

저장 용량 한도를 걸면서 이 길이 없으면 한도가 일방통행이 된다 — 찬 사람은 빠져나갈
방법이 없다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from conftest import TEST_PASSWORD, login, make_settings
from soriham_api import auth
from soriham_api.app import create_app
from soriham_api.models import JobLog, Recording, RecordingShare, Segment, SpeakerName
from soriham_api.storage import workspace_upload_dir
from soriham_api.tenancy import add_member, create_user


@pytest.fixture
def client(engine, db, owner, tmp_path: Path):
    app = create_app(
        settings=make_settings(upload_dir=tmp_path / "uploads", audio_dirs=(tmp_path / "rec",)),
        session_factory=sessionmaker(bind=engine),
    )
    c = TestClient(app)
    login(c, owner.email)
    return c


def make_upload(db, workspace, tmp_path: Path, name: str = "a.wav") -> Recording:
    dest = workspace_upload_dir(tmp_path / "uploads", workspace.public_id) / "2026-08" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"RIFF" + b"\x00" * 64)
    recording = Recording(
        workspace_id=workspace.id,
        source="upload",
        path=str(dest),
        filename=name,
        size_bytes=dest.stat().st_size,
        partial_hash=f"hash-{name}",
        status="done",
    )
    db.add(recording)
    db.flush()
    db.add(Segment(recording_id=recording.id, idx=0, start_sec=0.0, end_sec=1.0, text="안녕"))
    db.add(SpeakerName(recording_id=recording.id, speaker_key="SPEAKER_00", display_name="김"))
    db.add(
        JobLog(
            workspace_id=workspace.id,
            recording_id=recording.id,
            stage="transcribe",
            status="done",
            started_at=func.now(),
            audio_sec=120.0,
            elapsed_sec=30.0,
        )
    )
    db.commit()
    return recording


def make_scan(db, workspace, tmp_path: Path, name: str = "b.wav") -> Recording:
    src = tmp_path / "rec" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"RIFF" + b"\x00" * 64)
    recording = Recording(
        workspace_id=workspace.id,
        source="scan",
        path=str(src),
        filename=name,
        size_bytes=src.stat().st_size,
        partial_hash=f"hash-{name}",
        status="done",
    )
    db.add(recording)
    db.commit()
    return recording


def test_업로드본을_지우면_원본_파일도_사라진다(client, db, workspace, tmp_path: Path):
    recording = make_upload(db, workspace, tmp_path)
    path = Path(recording.path)

    assert client.delete(f"/api/recordings/{recording.public_id}").status_code == 204

    assert not path.exists()
    assert db.scalar(select(Recording).where(Recording.id == recording.id)) is None


def test_스캔본을_지워도_원본_파일은_남는다(client, db, workspace, tmp_path: Path):
    """소유자의 원본을 서비스가 지우지 않는다. 제자리 인덱싱의 전제다."""
    recording = make_scan(db, workspace, tmp_path)
    path = Path(recording.path)

    assert client.delete(f"/api/recordings/{recording.public_id}").status_code == 204

    assert path.exists()
    assert db.scalar(select(Recording).where(Recording.id == recording.id)) is None


def test_지우면_전사와_화자_이름도_함께_사라진다(client, db, workspace, tmp_path: Path):
    recording = make_upload(db, workspace, tmp_path)
    rec_id = recording.id

    client.delete(f"/api/recordings/{recording.public_id}")

    assert db.scalar(select(func.count(Segment.id)).where(Segment.recording_id == rec_id)) == 0
    assert (
        db.scalar(select(func.count(SpeakerName.id)).where(SpeakerName.recording_id == rec_id)) == 0
    )


def test_지워도_처리_이력은_남는다(client, db, workspace, tmp_path: Path):
    """남지 않으면 올리고-전사하고-지우고로 사용량 한도를 공짜로 되돌릴 수 있다."""
    recording = make_upload(db, workspace, tmp_path)

    client.delete(f"/api/recordings/{recording.public_id}")

    rows = db.scalars(select(JobLog).where(JobLog.workspace_id == workspace.id)).all()
    assert len(rows) == 1
    assert rows[0].recording_id is None
    assert rows[0].audio_sec == 120.0


def test_지우면_그_녹음의_공유도_사라진다(client, db, workspace, tmp_path: Path):
    recording = make_upload(db, workspace, tmp_path)
    guest = create_user(
        db,
        email="guest@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="손님",
        status="active",
    )
    db.add(RecordingShare(recording_id=recording.id, user_id=guest.id, permission="view"))
    db.commit()
    rec_id = recording.id

    client.delete(f"/api/recordings/{recording.public_id}")

    assert (
        db.scalar(
            select(func.count(RecordingShare.id)).where(RecordingShare.recording_id == rec_id)
        )
        == 0
    )


def test_열람자는_지울_수_없다(engine, db, workspace, tmp_path: Path):
    """지우기는 MANAGE다."""
    recording = make_upload(db, workspace, tmp_path)
    viewer = create_user(
        db,
        email="viewer@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="열람자",
        status="active",
    )
    add_member(db, workspace, viewer, "viewer")
    db.commit()

    app = create_app(
        settings=make_settings(upload_dir=tmp_path / "uploads"),
        session_factory=sessionmaker(bind=engine),
    )
    c = TestClient(app)
    login(c, viewer.email)

    assert c.delete(f"/api/recordings/{recording.public_id}").status_code == 404
    assert Path(recording.path).exists()


def test_남의_녹음은_지울_수_없다(engine, db, workspace, other_workspace, tmp_path: Path):
    recording = make_upload(db, workspace, tmp_path)
    stranger = create_user(
        db,
        email="stranger@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="남",
        status="active",
    )
    add_member(db, other_workspace, stranger, "owner")
    db.commit()

    app = create_app(
        settings=make_settings(upload_dir=tmp_path / "uploads"),
        session_factory=sessionmaker(bind=engine),
    )
    c = TestClient(app)
    login(c, stranger.email)

    assert c.delete(f"/api/recordings/{recording.public_id}").status_code == 404
    assert Path(recording.path).exists()


def test_CSRF_헤더가_없으면_지울_수_없다(client, db, workspace, tmp_path: Path):
    recording = make_upload(db, workspace, tmp_path)
    del client.headers["x-csrf-token"]

    assert client.delete(f"/api/recordings/{recording.public_id}").status_code == 403
    assert Path(recording.path).exists()


def test_뿌리_밖_경로는_삭제로도_건드리지_않는다(client, db, workspace, tmp_path: Path):
    """삭제가 경로 봉쇄를 우회하는 길이 되면 안 된다."""
    outside = tmp_path / "elsewhere" / "secret.wav"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"RIFF")
    recording = make_upload(db, workspace, tmp_path)
    recording.path = str(outside)
    db.commit()

    assert client.delete(f"/api/recordings/{recording.public_id}").status_code == 204

    assert outside.exists()
    assert db.scalar(select(Recording).where(Recording.id == recording.id)) is None


def test_파일이_이미_없어도_지워진다(client, db, workspace, tmp_path: Path):
    recording = make_upload(db, workspace, tmp_path)
    Path(recording.path).unlink()

    assert client.delete(f"/api/recordings/{recording.public_id}").status_code == 204
    assert db.scalar(select(Recording).where(Recording.id == recording.id)) is None


def test_편집_공유를_받아도_지울_수_없다(engine, db, workspace, tmp_path: Path):
    """공유로 얻는 권한은 편집이 상한이다. 지우기가 거기 걸리지 않으면, 공유받은
    사람이 남의 녹음을 없앨 수 있다."""
    recording = make_upload(db, workspace, tmp_path)
    guest = create_user(
        db,
        email="guest@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="손님",
        status="active",
    )
    db.add(RecordingShare(recording_id=recording.id, user_id=guest.id, permission="edit"))
    db.commit()

    app = create_app(
        settings=make_settings(upload_dir=tmp_path / "uploads"),
        session_factory=sessionmaker(bind=engine),
    )
    c = TestClient(app)
    login(c, guest.email)

    # 편집은 되지만
    assert (
        c.patch(f"/api/recordings/{recording.public_id}", json={"title": "고침"}).status_code == 200
    )
    # 지우지는 못한다
    assert c.delete(f"/api/recordings/{recording.public_id}").status_code == 404
    assert Path(recording.path).exists()


def test_원본을_지우면_중복으로_묶였던_녹음이_되살아난다(client, db, workspace, tmp_path: Path):
    """되살리지 않으면 원본 없는 중복으로 남아 워커가 집지 않고 재스캔도 못 살린다.
    파일은 폴더에 있는데 아카이브에는 내용이 없는 상태가 된다."""
    original = make_upload(db, workspace, tmp_path, "original.wav")
    dup = make_scan(db, workspace, tmp_path, "copy.wav")
    dup.status = "duplicate"
    dup.duplicate_of_id = original.id
    db.commit()

    assert client.delete(f"/api/recordings/{original.public_id}").status_code == 204

    db.refresh(dup)
    assert dup.status == "pending"
    assert dup.duplicate_of_id is None
