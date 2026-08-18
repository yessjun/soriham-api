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
    def __init__(
        self, result=None, error: Exception | None = None, progress: tuple[float, ...] = ()
    ) -> None:
        self.result = result or RESULT
        self.error = error
        self.calls: list[Path] = []
        self.progress = progress

    def transcribe(
        self,
        audio_path: Path,
        *,
        model,
        language,
        diarize,
        max_resubmits=2,
        on_progress=None,
    ):
        self.calls.append(audio_path)
        if on_progress is not None:
            for ratio in self.progress:
                on_progress("transcribe", ratio)
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


def test_전사_진행률이_저장되고_완료_시_정리된다(db, tmp_path: Path) -> None:
    [recording] = register(db, tmp_path, ["20260818_120000.wav"])
    runner = FakeRunnerClient(progress=(0.2, 0.5, 0.9))

    process_one(db, runner, model=None, language=None, enricher=None)

    db.refresh(recording)
    assert recording.status == "done"
    # 진행 정보는 끝나면 남기지 않는다 (목록에 100%가 박제되지 않게)
    assert recording.progress is None
    assert recording.stage_started_at is None


def test_진행률이_1퍼센트포인트_미만이면_쓰지_않는다(db, tmp_path: Path) -> None:
    """3초 폴링마다 커밋하지 않기 위한 임계값."""
    [recording] = register(db, tmp_path, ["20260818_130000.wav"])
    commits: list[float | None] = []

    runner = FakeRunnerClient(progress=(0.30, 0.302, 0.35))
    original = db.commit

    def spy() -> None:
        commits.append(recording.progress)
        original()

    db.commit = spy  # type: ignore[method-assign]
    try:
        process_one(db, runner, model=None, language=None, enricher=None)
    finally:
        db.commit = original  # type: ignore[method-assign]

    # 0.302는 0.30과 1%p 미만 차이라 건너뛰고, 0.30과 0.35만 반영된다
    assert 0.30 in commits
    assert 0.302 not in commits
    assert 0.35 in commits


def test_전사_실패하면_진행_정보를_정리한다(db, tmp_path: Path) -> None:
    [recording] = register(db, tmp_path, ["20260818_140000.wav"])
    runner = FakeRunnerClient(error=RuntimeError("러너 실패"), progress=(0.4,))

    process_one(db, runner, model=None, language=None, enricher=None)

    db.refresh(recording)
    assert recording.status == "error"
    assert recording.progress is None
    assert recording.stage_started_at is None


def test_화자분리로_넘어가면_진행률을_비운다(db, tmp_path: Path) -> None:
    """마지막 퍼센트를 그대로 두면 그 값에 멈춘 것처럼 보인다."""
    [recording] = register(db, tmp_path, ["20260818_180000.wav"])
    seen: list[float | None] = []

    class StageRunner(FakeRunnerClient):
        def transcribe(
            self, audio_path, *, model, language, diarize, max_resubmits=2, on_progress=None
        ):
            self.calls.append(audio_path)
            on_progress("transcribe", 0.6)
            seen.append(recording.progress)
            on_progress("diarize", None)
            seen.append(recording.progress)
            return self.result

    process_one(db, StageRunner(), model=None, language=None, enricher=None)

    assert seen == [0.6, None]
