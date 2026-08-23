"""사용량 한도.

승인이 곧 무제한이 아니다. 실제 비용은 GPU 전사 시간이고 저장 용량이 그다음이다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from conftest import login, make_settings
from soriham_api.app import create_app
from soriham_api.models import JobLog, Recording
from soriham_api.quota import QuotaExceeded, check_minutes, check_storage, measure
from soriham_api.worker import process_one, release_quota_blocked
from test_worker import FakeRunnerClient

WAV = b"RIFF" + b"\x00" * 2048


@pytest.fixture
def client(engine, db, owner, tmp_path: Path):
    app = create_app(
        settings=make_settings(upload_dir=tmp_path / "uploads"),
        session_factory=sessionmaker(bind=engine),
    )
    c = TestClient(app)
    login(c, owner.email)
    return c


def log_transcribe(db, workspace, *, minutes: float, days_ago: float = 0) -> JobLog:
    started = datetime.now(UTC) - timedelta(days=days_ago)
    row = JobLog(
        workspace_id=workspace.id,
        recording_id=None,
        stage="transcribe",
        status="done",
        started_at=started,
        finished_at=started,
        audio_sec=minutes * 60.0,
        elapsed_sec=minutes * 6.0,
    )
    db.add(row)
    db.commit()
    return row


def make_recording(
    db, workspace, *, size: int = 1024, duration: float | None = 600.0, name="a.wav"
):
    recording = Recording(
        workspace_id=workspace.id,
        source="upload",
        path=f"/tmp/{workspace.slug}/{name}",
        filename=name,
        size_bytes=size,
        partial_hash=f"{workspace.slug}-{name}",
        duration_sec=duration,
        status="pending",
    )
    db.add(recording)
    db.commit()
    return recording


def test_한도가_비어_있으면_무제한이다(db, workspace):
    """주인의 스캔 워크스페이스가 이 상태다. 1만 시간 백로그가 자기 한도에 걸리면 안 된다."""
    log_transcribe(db, workspace, minutes=100_000)
    usage = measure(db, workspace)

    assert usage.minutes_left is None
    assert usage.bytes_left is None
    check_minutes(usage, 3600.0)
    check_storage(usage, 10**12)


def test_길이를_몰라도_한도가_없으면_받는다(db, workspace):
    """무제한인데 길이를 못 읽었다고 막으면 백로그의 별난 파일이 통째로 거절된다."""
    check_minutes(measure(db, workspace), None)


def test_한도가_있는데_길이를_모르면_거절한다(db, workspace):
    """모르면 한도를 강제할 방법이 없고, 합계가 빈 값을 건너뛰어 영원히 공짜가 된다."""
    workspace.quota_minutes = 60
    db.commit()
    with pytest.raises(QuotaExceeded):
        check_minutes(measure(db, workspace), None)


def test_남은_시간보다_긴_녹음은_거절한다(db, workspace):
    workspace.quota_minutes = 100
    db.commit()
    log_transcribe(db, workspace, minutes=90)
    usage = measure(db, workspace)

    assert usage.used_minutes == pytest.approx(90.0)
    check_minutes(usage, 9 * 60)
    with pytest.raises(QuotaExceeded, match="남은 시간"):
        check_minutes(usage, 11 * 60)


def test_창_밖의_이력은_세지_않는다(db, workspace):
    """30일 롤링이라 기간이 지나면 회복된다."""
    workspace.quota_minutes = 100
    db.commit()
    log_transcribe(db, workspace, minutes=90, days_ago=31)

    assert measure(db, workspace).used_minutes == pytest.approx(0.0)


def test_남의_워크스페이스_사용량은_섞이지_않는다(db, workspace, other_workspace):
    log_transcribe(db, other_workspace, minutes=500)
    assert measure(db, workspace).used_minutes == pytest.approx(0.0)


def test_사라진_녹음과_중복은_자리를_차지하지_않는다(db, workspace):
    make_recording(db, workspace, size=1000, name="a.wav")
    gone = make_recording(db, workspace, size=9_000_000, name="b.wav")
    gone.status = "missing"
    dup = make_recording(db, workspace, size=9_000_000, name="c.wav")
    dup.status = "duplicate"
    db.commit()

    assert measure(db, workspace).used_bytes == 1000


def test_용량이_모자라면_거절한다(db, workspace):
    workspace.quota_bytes = 10_000
    db.commit()
    make_recording(db, workspace, size=9_000)
    usage = measure(db, workspace)

    check_storage(usage, 500)
    with pytest.raises(QuotaExceeded, match="저장 공간"):
        check_storage(usage, 5_000)


def test_업로드가_용량_한도에_걸리면_파일이_남지_않는다(client, db, workspace, tmp_path):
    """자리를 잡기 전에 막아야 한다. 뒤에 막으면 파일만 남고 행은 없다."""
    workspace.quota_bytes = 1024
    db.commit()

    resp = client.post(
        f"/api/workspaces/{workspace.public_id}/recordings",
        files={"file": ("20260817_100000.wav", WAV, "audio/wav")},
    )

    assert resp.status_code == 413
    assert "저장 공간" in resp.json()["detail"]
    assert db.scalar(select(Recording)) is None
    staging = tmp_path / "uploads" / ".incoming"
    assert not staging.is_dir() or list(staging.iterdir()) == []


def test_워커가_한도를_넘으면_러너에_보내지_않는다(db, workspace, tmp_path):
    """업로드 검사를 스캔 유입분이 우회하므로 여기가 권위 있는 관문이다."""
    workspace.quota_minutes = 10
    db.commit()
    make_recording(db, workspace, duration=3600.0)
    runner = FakeRunnerClient()

    assert process_one(db, runner) is True

    assert runner.calls == []
    assert db.scalar(select(Recording.status)) == "quota_blocked"


def test_한도가_풀리면_큐로_되돌아온다(db, workspace, tmp_path):
    """고장이 아니라 풀리는 상태다. 워커 재시작 없이 회복돼야 한다."""
    workspace.quota_minutes = 10
    db.commit()
    recording = make_recording(db, workspace, duration=3600.0)
    process_one(db, FakeRunnerClient())
    assert recording.status == "quota_blocked"

    workspace.quota_minutes = 1000
    db.commit()

    assert release_quota_blocked(db) == 1
    db.refresh(recording)
    assert recording.status == "pending"


def test_한도가_그대로면_되돌리지_않는다(db, workspace):
    workspace.quota_minutes = 10
    db.commit()
    make_recording(db, workspace, duration=3600.0)
    process_one(db, FakeRunnerClient())

    assert release_quota_blocked(db) == 0


def test_사용량_조회는_구성원에게_열려_있다(client, db, workspace):
    workspace.quota_minutes = 600
    workspace.quota_bytes = 1024 * 1024
    db.commit()
    log_transcribe(db, workspace, minutes=30)

    body = client.get(f"/api/workspaces/{workspace.public_id}/usage").json()

    assert body["used_minutes"] == pytest.approx(30.0)
    assert body["quota_minutes"] == 600
    assert body["window_days"] == 30


def test_남의_워크스페이스_사용량은_볼_수_없다(client, other_workspace):
    resp = client.get(f"/api/workspaces/{other_workspace.public_id}/usage")
    assert resp.status_code == 404


def test_워커가_유휴일_때_해제를_본다(engine, db, workspace, monkeypatch):
    """해제 함수가 있어도 루프가 부르지 않으면 아무 소용이 없다.

    루프를 끊는 역할은 sleep에 맡긴다. 해제 호출에 맡기면 호출부가 사라졌을 때
    테스트가 실패하는 대신 멈춰 버린다.
    """
    from sqlalchemy.orm import sessionmaker as sm

    from soriham_api import worker

    called = []
    monkeypatch.setattr(worker, "idle_maintenance", lambda _: called.append(True))
    monkeypatch.setattr(worker.time, "sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt))

    worker.run_worker(sm(bind=engine), FakeRunnerClient(), idle_sleep_sec=0.01)

    assert called == [True]
