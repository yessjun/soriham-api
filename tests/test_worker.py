from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from soriham_api.ingest import scan
from soriham_api.models import JobLog, Recording, Segment
from soriham_api.worker import claim_next, process_one, recover_in_flight

RESULT = {
    "language": "ko",
    "segments": [
        {
            "start": 0.0,
            "end": 1.0,
            "text": "안녕하세요",
            "speaker": "SPEAKER_00",
            "words": [["안녕하세요", 0.0, 1.0]],
        },
        {"start": 1.2, "end": 2.0, "text": "반갑습니다", "speaker": "SPEAKER_01", "words": []},
    ],
    "meta": {"device": "mlx", "model": "large-v3-turbo", "elapsed_sec": 3.2},
}


class FakeRunnerClient:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result or RESULT
        self.error = error
        self.calls: list[Path] = []

    def transcribe(self, audio_path: Path, *, model, language, diarize, max_resubmits=2):
        self.calls.append(audio_path)
        if self.error is not None:
            raise self.error
        return self.result


def register(db, tmp_path: Path, names: list[str]) -> list[Recording]:
    for name in names:
        p = tmp_path / "rec" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(name.encode())
    scan(db, (tmp_path / "rec",))
    return db.scalars(select(Recording).order_by(Recording.id)).all()


def test_claim_prefers_latest_recording(db, tmp_path: Path):
    register(db, tmp_path, ["20260101_000000.wav", "20260817_000000.wav"])
    claimed = claim_next(db)
    assert claimed is not None
    assert claimed.filename == "20260817_000000.wav"


def test_process_one_saves_segments_and_logs(db, tmp_path: Path):
    register(db, tmp_path, ["a.wav"])
    runner = FakeRunnerClient()

    assert process_one(db, runner) is True
    row = db.scalars(select(Recording)).one()
    assert row.status == "done"
    assert row.language == "ko"
    assert row.stt_meta["device"] == "mlx"
    segments = db.scalars(select(Segment).order_by(Segment.idx)).all()
    assert [s.text for s in segments] == ["안녕하세요", "반갑습니다"]
    assert segments[0].speaker_key == "SPEAKER_00"
    log = db.scalars(select(JobLog)).one()
    assert (log.stage, log.status) == ("transcribe", "done")
    assert log.elapsed_sec == 3.2

    # 큐가 비면 False
    assert process_one(db, runner) is False


def test_process_one_replaces_segments_on_rerun(db, tmp_path: Path):
    register(db, tmp_path, ["a.wav"])
    process_one(db, FakeRunnerClient())
    row = db.scalars(select(Recording)).one()
    row.status = "pending"
    db.commit()

    process_one(db, FakeRunnerClient())
    segments = db.scalars(select(Segment)).all()
    assert len(segments) == 2  # 중복 누적 없이 교체


def test_process_one_isolates_error_and_continues(db, tmp_path: Path):
    register(db, tmp_path, ["20260101_000000.wav", "20260817_000000.wav"])
    failing = FakeRunnerClient(error=RuntimeError("러너 다운"))

    assert process_one(db, failing) is True
    failed = db.scalars(select(Recording).where(Recording.status == "error")).one()
    assert "러너 다운" in failed.error
    assert failed.filename == "20260817_000000.wav"

    # 다음 호출은 남은 레코드를 정상 처리
    assert process_one(db, FakeRunnerClient()) is True
    assert db.scalars(select(Recording).where(Recording.status == "done")).one() is not None


def test_recover_in_flight_uses_checkpoints(db, tmp_path: Path):
    rows = register(db, tmp_path, ["a.wav", "b.wav"])
    rows[0].status = "transcribing"  # 세그먼트 없음 -> pending
    rows[1].status = "enriching"
    rows[1].segments.append(Segment(idx=0, start_sec=0, end_sec=1, text="x"))
    db.commit()

    assert recover_in_flight(db) == 2
    db.refresh(rows[0])
    db.refresh(rows[1])
    assert rows[0].status == "pending"
    assert rows[1].status == "enriching"


def test_enriching_recording_skips_transcribe(db, tmp_path: Path):
    register(db, tmp_path, ["a.wav"])
    runner = FakeRunnerClient()
    process_one(db, runner)
    row = db.scalars(select(Recording)).one()
    row.status = "enriching"
    db.commit()

    process_one(db, runner)
    db.refresh(row)
    assert row.status == "done"
    assert len(runner.calls) == 1  # 전사는 다시 돌지 않음
