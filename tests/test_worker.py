from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select, update

from soriham_api.ingest import scan
from soriham_api.models import JobLog, Recording, Segment
from soriham_api.worker import (
    HEARTBEAT_EVERY,
    claim_next,
    process_one,
    recover_in_flight,
)

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
        timeout_sec=None,
    ):
        self.calls.append(audio_path)
        if on_progress is not None:
            for ratio in self.progress:
                on_progress("transcribe", ratio)
        if self.error is not None:
            raise self.error
        return self.result


def register(db, tmp_path: Path, names: list[str], workspace) -> list[Recording]:
    for name in names:
        p = tmp_path / "rec" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(name.encode())
    scan(db, (tmp_path / "rec",), workspace_id=workspace.id)
    return db.scalars(select(Recording).order_by(Recording.id)).all()


def test_claim_prefers_latest_recording(db, tmp_path: Path, workspace):
    register(db, tmp_path, ["20260101_000000.wav", "20260817_000000.wav"], workspace)
    claimed = claim_next(db)
    assert claimed is not None
    assert claimed.filename == "20260817_000000.wav"


def test_process_one_saves_segments_and_logs(db, tmp_path: Path, workspace):
    register(db, tmp_path, ["a.wav"], workspace)
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


def test_process_one_replaces_segments_on_rerun(db, tmp_path: Path, workspace):
    register(db, tmp_path, ["a.wav"], workspace)
    process_one(db, FakeRunnerClient())
    row = db.scalars(select(Recording)).one()
    row.status = "pending"
    db.commit()

    process_one(db, FakeRunnerClient())
    segments = db.scalars(select(Segment)).all()
    assert len(segments) == 2  # 중복 누적 없이 교체


def test_process_one_isolates_error_and_continues(db, tmp_path: Path, workspace):
    register(db, tmp_path, ["20260101_000000.wav", "20260817_000000.wav"], workspace)
    failing = FakeRunnerClient(error=RuntimeError("러너 다운"))

    assert process_one(db, failing) is True
    failed = db.scalars(select(Recording).where(Recording.status == "error")).one()
    assert "러너 다운" in failed.error
    assert failed.filename == "20260817_000000.wav"

    # 다음 호출은 남은 레코드를 정상 처리
    assert process_one(db, FakeRunnerClient()) is True
    assert db.scalars(select(Recording).where(Recording.status == "done")).one() is not None


def test_상한을_넘긴_잡은_에러로_세우고_러너에서_물러난다(db, tmp_path: Path, workspace):
    """물린 러너 앞에서 다음 녹음을 바로 집으면 대기열이 통째로 상한을 태운다."""
    from soriham_api.stt_client import RunnerJobTimedOut, RunnerUnavailable

    register(db, tmp_path, ["a.wav"], workspace)
    stuck = FakeRunnerClient(error=RunnerJobTimedOut("러너 잡이 제한 시간을 넘김: job-1"))

    with pytest.raises(RunnerUnavailable):
        process_one(db, stuck)

    row = db.scalars(select(Recording)).one()
    assert row.status == "error"
    assert "제한 시간" in row.error


def test_상한은_녹음_길이에_비례한다(db, tmp_path: Path, workspace):
    from soriham_api.worker import JOB_TIMEOUT_FLOOR_SEC, job_timeout_sec

    row = register(db, tmp_path, ["a.wav"], workspace)[0]
    row.duration_sec = 10.0
    assert job_timeout_sec(row) == JOB_TIMEOUT_FLOOR_SEC  # 짧은 녹음은 바닥값
    row.duration_sec = 6 * 3600.0
    assert job_timeout_sec(row) == 6 * 3600.0 * 20.0


def test_recover_in_flight_uses_checkpoints(db, tmp_path: Path, workspace):
    from datetime import UTC, datetime, timedelta

    rows = register(db, tmp_path, ["a.wav", "b.wav"], workspace)
    rows[0].status = "transcribing"  # 세그먼트 없음 -> pending
    rows[1].status = "summarizing"  # 세그먼트까지 저장됨 -> 요약 대기
    rows[1].segments.append(Segment(idx=0, start_sec=0, end_sec=1, text="x"))
    db.commit()
    # 최근 기록이 남은 것은 살아 있는 작업으로 본다. 오래된 것만 되돌린다
    old = datetime.now(UTC) - timedelta(hours=1)
    for row in rows:
        row.updated_at = old
    db.commit()

    assert recover_in_flight(db) == 2
    db.refresh(rows[0])
    db.refresh(rows[1])
    assert rows[0].status == "pending"
    assert rows[1].status == "enriching"


def test_요약을_기다리는_녹음은_재개_대상이_아니다(db, tmp_path: Path, workspace):
    """enriching은 대기 상태다. 워커가 그냥 집으면 되지 되돌릴 것이 없다."""
    rows = register(db, tmp_path, ["a.wav"], workspace)
    rows[0].status = "enriching"
    rows[0].segments.append(Segment(idx=0, start_sec=0, end_sec=1, text="x"))
    db.commit()

    assert recover_in_flight(db) == 0
    db.refresh(rows[0])
    assert rows[0].status == "enriching"


def test_enriching_recording_skips_transcribe(db, tmp_path: Path, workspace):
    register(db, tmp_path, ["a.wav"], workspace)
    runner = FakeRunnerClient()
    process_one(db, runner)
    row = db.scalars(select(Recording)).one()
    row.status = "enriching"
    db.commit()

    process_one(db, runner)
    db.refresh(row)
    assert row.status == "done"
    assert len(runner.calls) == 1  # 전사는 다시 돌지 않음


def test_전사_진행률이_저장되고_완료_시_정리된다(db, tmp_path: Path, workspace) -> None:
    [recording] = register(db, tmp_path, ["20260818_120000.wav"], workspace)
    runner = FakeRunnerClient(progress=(0.2, 0.5, 0.9))

    process_one(db, runner, model=None, language=None, enricher=None)

    db.refresh(recording)
    assert recording.status == "done"
    # 진행 정보는 끝나면 남기지 않는다 (목록에 100%가 박제되지 않게)
    assert recording.progress is None
    assert recording.stage_started_at is None


def test_진행률이_1퍼센트포인트_미만이면_쓰지_않는다(db, tmp_path: Path, workspace) -> None:
    """3초 폴링마다 커밋하지 않기 위한 임계값."""
    [recording] = register(db, tmp_path, ["20260818_130000.wav"], workspace)
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


def test_전사_실패하면_진행_정보를_정리한다(db, tmp_path: Path, workspace) -> None:
    [recording] = register(db, tmp_path, ["20260818_140000.wav"], workspace)
    runner = FakeRunnerClient(error=RuntimeError("러너 실패"), progress=(0.4,))

    process_one(db, runner, model=None, language=None, enricher=None)

    db.refresh(recording)
    assert recording.status == "error"
    assert recording.progress is None
    assert recording.stage_started_at is None


def test_화자분리로_넘어가면_진행률을_비운다(db, tmp_path: Path, workspace) -> None:
    """마지막 퍼센트를 그대로 두면 그 값에 멈춘 것처럼 보인다."""
    [recording] = register(db, tmp_path, ["20260818_180000.wav"], workspace)
    seen: list[float | None] = []

    class StageRunner(FakeRunnerClient):
        def transcribe(
            self,
            audio_path,
            *,
            model,
            language,
            diarize,
            max_resubmits=2,
            on_progress=None,
            timeout_sec=None,
        ):
            self.calls.append(audio_path)
            on_progress("transcribe", 0.6)
            seen.append(recording.progress)
            on_progress("diarize", None)
            seen.append(recording.progress)
            return self.result

    process_one(db, StageRunner(), model=None, language=None, enricher=None)

    assert seen == [0.6, None]


def test_화자분리_실패는_화면에_남는다(db, tmp_path: Path, workspace) -> None:
    """녹취록은 살리되 조용히 넘기지 않는다. 엔리치먼트가 성공해도 지워지지 않는다."""
    [recording] = register(db, tmp_path, ["20260818_190000.wav"], workspace)
    result = dict(RESULT)
    result["meta"] = {"diarized": False, "diarize_error": "RuntimeError: 디코더 실패"}

    class Enricher:
        def enrich(self, session, rec, **_) -> None:
            rec.summary = "요약"

    process_one(db, FakeRunnerClient(result=result), model=None, language=None, enricher=Enricher())

    db.refresh(recording)
    assert recording.status == "done"
    assert recording.summary == "요약"
    assert recording.error is not None
    assert "디코더 실패" in recording.error


def test_소음_세그먼트는_요약에_들어가지_않는다(db, tmp_path: Path, workspace) -> None:
    [recording] = register(db, tmp_path, ["20260818_200000.wav"], workspace)
    result = dict(RESULT)
    result["segments"] = [
        {"start": 0.0, "end": 1.0, "text": "실제 발언", "speaker": None, "kind": "speech"},
        {"start": 1.0, "end": 5.0, "text": "", "speaker": None, "kind": "noise"},
    ]
    captured: list[str] = []

    class Enricher:
        def enrich(self, session, rec, **_) -> None:
            from soriham_api.enrich import build_transcript

            captured.append(build_transcript(session, rec))

    process_one(db, FakeRunnerClient(result=result), model=None, language=None, enricher=Enricher())

    db.refresh(recording)
    assert [s.kind for s in recording.segments] == ["speech", "noise"]
    assert captured == ["실제 발언"]


def test_처리_중에_녹음이_사라져도_워커가_멎지_않는다(db, tmp_path: Path, workspace):
    """삭제 기능이 생기면서 실제로 도달 가능해진 자리다.

    결과는 버려지되 워커는 다음 레코드로 넘어가야 한다. 실패가 격리를 뚫고 나가면
    루프가 잠들었다 깨는 동안 큐 전체가 멈춘다.
    """
    [recording] = register(db, tmp_path, ["20260818_210000.wav"], workspace)

    class VanishingRunner(FakeRunnerClient):
        def transcribe(self, audio_path, **kwargs):
            # 러너가 도는 사이 다른 세션이 녹음을 지운 상황
            db.execute(delete(Recording).where(Recording.id == recording.id))
            db.commit()
            return super().transcribe(audio_path, **kwargs)

    # 예외가 밖으로 새면 여기서 터진다
    assert process_one(db, VanishingRunner()) is True
    assert db.scalar(select(Recording).where(Recording.id == recording.id)) is None


def test_옆_워커가_붙어_있는_작업은_되돌리지_않는다(db, tmp_path: Path, workspace):
    """무조건 되돌리면 워커 하나를 재시작할 때 옆 워커의 작업을 빼앗는다.

    같은 오디오가 두 번 전사되고, 두 워커가 동시에 세그먼트를 갈아 끼우면 한쪽이
    완료된 녹음을 error로 덮는다.
    """
    rows = register(db, tmp_path, ["a.wav"], workspace)
    rows[0].status = "transcribing"
    db.commit()

    assert recover_in_flight(db) == 0
    db.refresh(rows[0])
    assert rows[0].status == "transcribing"


def test_러너에_못_닿으면_큐로_되돌린다(db, tmp_path: Path, workspace):
    """러너가 죽어 있는 동안 대기열 전체가 error가 되면 되돌릴 길이 사람 손뿐이다."""
    from soriham_api.stt_client import RunnerUnavailable

    register(db, tmp_path, ["a.wav"], workspace)

    with pytest.raises(RunnerUnavailable):
        process_one(db, FakeRunnerClient(error=RunnerUnavailable("연결 거부")))

    row = db.scalars(select(Recording)).one()
    assert row.status == "pending"
    assert row.error is None


def test_러너_잡_실패는_그_녹음만_error로_둔다(db, tmp_path: Path, workspace):
    """러너가 처리하고 실패한 것은 이 파일의 문제다. 큐로 되돌릴 이유가 없다."""
    from soriham_api.stt_client import RunnerJobFailed

    register(db, tmp_path, ["a.wav"], workspace)

    process_one(db, FakeRunnerClient(error=RunnerJobFailed("디코딩 실패")))

    row = db.scalars(select(Recording)).one()
    assert row.status == "error"
    assert "디코딩 실패" in row.error


def test_실패한_녹음을_다시_큐에_넣는다(db, tmp_path: Path, workspace):
    """error에서 나가는 길이 삭제뿐이면, 러너가 잠깐 죽은 사이 실패한 수천 건을
    손으로 지우고 다시 올려야 한다."""
    from soriham_api.worker import retry_failed

    rows = register(db, tmp_path, ["a.wav", "b.wav", "c.wav"], workspace)
    for row in rows:
        row.status = "error"
        row.error = "stt: 연결 거부"
    rows[1].segments.append(Segment(idx=0, start_sec=0, end_sec=1, text="x"))
    rows[2].status = "error"
    rows[2].summary = "이미 요약까지 끝났다"
    db.commit()

    assert retry_failed(db) == 3

    for row in rows:
        db.refresh(row)
    # 재개 지점은 남아 있는 산출물이 정한다. 이미 치른 GPU 시간을 다시 쓰지 않는다
    assert rows[0].status == "pending"
    assert rows[1].status == "enriching"
    assert rows[2].status == "done"
    assert rows[0].error is None


def test_다시_시도는_워크스페이스로_좁힐_수_있다(db, tmp_path: Path, workspace, other_workspace):
    from soriham_api.worker import retry_failed

    rows = register(db, tmp_path, ["a.wav"], workspace)
    rows[0].status = "error"
    theirs = Recording(
        workspace_id=other_workspace.id,
        source="upload",
        path="/tmp/theirs/x.wav",
        filename="x.wav",
        size_bytes=10,
        partial_hash="theirs-x",
        status="error",
    )
    db.add(theirs)
    db.commit()

    assert retry_failed(db, workspace_id=workspace.id) == 1

    db.refresh(theirs)
    assert theirs.status == "error"


def test_정리_주기가_멈춘_작업도_회수한다(db, tmp_path: Path, workspace, engine):
    """기동할 때만 회수하면, 단계 중간에 상태가 굳은 행은 워커를 다시 띄울 때까지
    처리 중으로 남는다. 백로그를 몇 주씩 도는 동안 그럴 일이 없다."""
    from sqlalchemy.orm import sessionmaker

    from soriham_api.worker import idle_maintenance

    rows = register(db, tmp_path, ["a.wav"], workspace)
    rows[0].status = "transcribing"
    db.commit()
    db.execute(
        update(Recording)
        .where(Recording.id == rows[0].id)
        .values(updated_at=datetime.now(UTC) - timedelta(hours=1))
    )
    db.commit()

    idle_maintenance(sessionmaker(bind=engine))

    db.refresh(rows[0])
    assert rows[0].status == "pending"
    assert rows[0].progress is None


def test_요약이_길어도_살아_있다고_알린다(db, tmp_path: Path, workspace):
    """요약은 LLM 호출이라 청크마다 몇 분씩 걸린다. 아무 기록도 안 남기면 옆 워커가
    죽은 작업으로 보고 같은 녹취록을 두 번 요약한다."""
    beats: list[datetime] = []

    class SlowEnricher:
        def enrich(self, session, recording, *, on_step=None) -> None:
            if on_step is not None:
                # 하트비트 간격을 넘긴 척한다
                recording.updated_at = datetime.now(UTC) - HEARTBEAT_EVERY * 2
                session.commit()
                on_step()
                beats.append(recording.updated_at)
            recording.summary = "요약"

    register(db, tmp_path, ["a.wav"], workspace)
    assert process_one(db, FakeRunnerClient(), enricher=SlowEnricher()) is True

    assert beats, "요약 도중에 하트비트를 받지 못했다"
    row = db.scalars(select(Recording)).one()
    assert datetime.now(UTC) - row.updated_at < HEARTBEAT_EVERY
