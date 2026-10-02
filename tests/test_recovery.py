from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import JSON, select, update

from soriham_api import worker
from soriham_api.models import JobLog, Recording
from soriham_api.stt_client import RunnerClient, RunnerUnavailable
from test_worker import RESULT, FakeRunnerClient, register


def test_usage_and_transcript_commit_together(db, workspace, tmp_path, monkeypatch):
    [row] = register(db, tmp_path, ["a.wav"], workspace)
    original = worker._log_stage

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "_log_stage", crash)
    with pytest.raises(KeyboardInterrupt):
        worker.process_one(db, FakeRunnerClient())
    db.rollback()
    assert db.scalars(select(JobLog)).all() == []
    monkeypatch.setattr(worker, "_log_stage", original)
    worker.recover_in_flight(db, now=datetime.now(UTC) + timedelta(minutes=6))
    worker.process_one(db, FakeRunnerClient())
    assert db.scalars(select(JobLog)).one().status == "done"
    assert db.scalars(select(Recording)).one().id == row.id


def test_worker_restart_reuses_accepted_request(db, workspace, tmp_path):
    [row] = register(db, tmp_path, ["a.wav"], workspace)
    requests = set()
    submissions = []
    crash = [True]

    def handler(request):
        if request.method == "POST":
            data = parse_qs(request.content.decode())
            request_id = data.get("request_id", ["unkeyed-" + str(len(submissions))])[0]
            requests.add(request_id)
            submissions.append(request_id)
            if crash[0]:
                crash[0] = False
                # 접수됐지만 응답을 DB에 적기 전에 프로세스가 죽은 경우
                raise KeyboardInterrupt
            return httpx.Response(200, json={"job_id": request_id.replace("-", "")})
        return httpx.Response(200, json={"status": "done", "result": RESULT})

    runner = RunnerClient("http://runner.test", transport=httpx.MockTransport(handler))
    with pytest.raises(KeyboardInterrupt):
        worker.process_one(db, runner)
    db.rollback()
    worker.recover_in_flight(db, now=datetime.now(UTC) + timedelta(minutes=6))
    worker.process_one(db, runner)
    db.refresh(row)
    assert row.status == "done"
    assert len(submissions) == 2
    assert len(requests) == 1
    assert len(db.scalars(select(JobLog)).all()) == 1


def test_connection_loss_keeps_request_and_deadline(db, workspace, tmp_path):
    [row] = register(db, tmp_path, ["a.wav"], workspace)
    runner = FakeRunnerClient(error=RunnerUnavailable("연결 중단"))
    for _ in range(2):
        with pytest.raises(RunnerUnavailable):
            worker.process_one(db, runner)
        if len(runner.calls) == 1:
            checkpoint = (row.runner_request_id, row.runner_started_at)
        assert (row.runner_request_id, row.runner_started_at) == checkpoint
    assert row.status == "pending"
    assert db.scalars(select(JobLog)).all() == []


@pytest.mark.parametrize("segments", [RESULT["segments"], []])
def test_summary_failure_restarts_from_transcript(db, workspace, tmp_path, segments):
    [row] = register(db, tmp_path, ["a.wav"], workspace)

    class Enricher:
        fail = True

        def enrich(self, session, recording, **kwargs):
            if self.fail:
                raise RuntimeError("요약 실패 주입")
            recording.summary = "합성 요약"

    enrich = Enricher()
    runner = FakeRunnerClient(result={**RESULT, "segments": segments})
    worker.process_one(db, runner, enricher=enrich)
    assert row.status == "done" and row.summary is None
    assert worker.requeue_unenriched(db) == 1
    enrich.fail = False
    worker.process_one(db, runner, enricher=enrich)
    db.refresh(row)
    assert row.summary == "합성 요약" and row.error is None
    assert len(runner.calls) == 1
    logs = db.scalars(select(JobLog).order_by(JobLog.id)).all()
    assert [(log.stage, log.status) for log in logs] == [
        ("transcribe", "done"),
        ("enrich", "error"),
        ("enrich", "done"),
    ]


def test_expired_deadline_does_not_submit(tmp_path):
    calls = []
    client = RunnerClient(
        "http://runner.test", transport=httpx.MockTransport(lambda req: calls.append(req))
    )
    from soriham_api.stt_client import RunnerJobTimedOut

    with pytest.raises(RunnerJobTimedOut):
        client.transcribe(
            Path(tmp_path / "a.wav"), model=None, language=None, diarize=False, timeout_sec=0
        )
    assert calls == []


def test_json_null_metadata_is_not_a_transcript(db, workspace, tmp_path):
    [row] = register(db, tmp_path, ["a.wav"], workspace)
    row.status = "done"
    db.execute(update(Recording).where(Recording.id == row.id).values(stt_meta=JSON.NULL))
    db.commit()
    assert worker.requeue_unenriched(db) == 0
