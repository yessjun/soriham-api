"""처리 워커: 큐 소비 → stt 러너 호출 → 세그먼트 저장 → 엔리치먼트 → done.

체크포인트 원칙: 단계 산출물(세그먼트, 요약)이 DB에 남으므로 강제 종료 후
재시작해도 남은 단계부터 이어서 처리한다.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from soriham_api.ingest import resume_status
from soriham_api.models import JobLog, Recording, Segment
from soriham_api.stt_client import RunnerClient

logger = logging.getLogger(__name__)

# 워커가 잡을 수 있는 대기 상태와, 재시작 시 재개 대상인 진행 중 상태
CLAIMABLE = ("pending",)
IN_FLIGHT = ("transcribing", "diarizing", "enriching")


class Enricher(Protocol):
    """제목·요약·태그 생성기. 엔리치먼트 단계에서 구현체가 붙는다."""

    def enrich(self, session: Session, recording: Recording) -> None: ...


def recover_in_flight(session: Session) -> int:
    """진행 중 상태로 남은(이전 실행이 중단된) 레코드를 체크포인트 기준으로 되돌린다."""
    count = 0
    for recording in session.scalars(select(Recording).where(Recording.status.in_(IN_FLIGHT))):
        recording.status = resume_status(recording)
        if recording.status == "enriching":
            # 세그먼트까지는 저장된 상태 — 엔리치먼트부터 재개
            pass
        count += 1
        logger.info("재개: %s -> %s", recording.filename, recording.status)
    session.commit()
    return count


def requeue_unenriched(session: Session) -> int:
    """요약 없이 done인 레코드를 엔리치먼트 대상으로 되돌린다(백필·재시도)."""
    count = 0
    for recording in session.scalars(
        select(Recording).where(
            Recording.status == "done",
            Recording.summary.is_(None),
        )
    ):
        if recording.segments:
            recording.status = "enriching"
            count += 1
    session.commit()
    return count


def claim_next(session: Session) -> Recording | None:
    """우선순위(최신 녹음 먼저)로 다음 대기 레코드를 집는다. 다중 워커 안전."""
    recording = session.scalars(
        select(Recording)
        .where(Recording.status.in_(CLAIMABLE))
        .order_by(Recording.recorded_at.desc().nulls_last(), Recording.id.desc())
        .with_for_update(skip_locked=True)
        .limit(1)
    ).first()
    if recording is None:
        # 세그먼트는 있는데 엔리치먼트가 안 끝난 레코드(enriching 재개분)
        recording = session.scalars(
            select(Recording)
            .where(Recording.status == "enriching")
            .order_by(Recording.recorded_at.desc().nulls_last(), Recording.id.desc())
            .with_for_update(skip_locked=True)
            .limit(1)
        ).first()
    return recording


def _log_stage(
    session: Session,
    recording: Recording,
    stage: str,
    started: datetime,
    *,
    status: str,
    meta: dict | None = None,
    error: str | None = None,
) -> None:
    meta = meta or {}
    session.add(
        JobLog(
            recording_id=recording.id,
            stage=stage,
            status=status,
            started_at=started,
            finished_at=datetime.now(UTC),
            audio_sec=recording.duration_sec,
            elapsed_sec=meta.get("elapsed_sec"),
            device=meta.get("device"),
            model=meta.get("model"),
            error=error,
        )
    )


def transcribe_stage(
    session: Session,
    recording: Recording,
    runner: RunnerClient,
    *,
    model: str | None,
    language: str | None,
) -> None:
    """러너 호출 후 세그먼트를 저장한다(재실행 대비 기존 세그먼트 교체)."""
    started = datetime.now(UTC)
    recording.status = "transcribing"
    recording.stage_started_at = started
    recording.progress = None
    session.commit()

    def report(stage: str | None, ratio: float | None) -> None:
        # 3초 폴링마다 쓰지 않는다 — 1%p 이상 움직였을 때만
        if ratio is None or (
            recording.progress is not None and abs(ratio - recording.progress) < 0.01
        ):
            return
        recording.progress = ratio
        session.commit()

    try:
        result = runner.transcribe(
            Path(recording.path),
            model=model,
            language=language,
            diarize=True,
            on_progress=report,
        )
        # 결과 파싱·저장 실패도 같은 에러 경로로 — transcribing 상태로 방치되지 않게
        session.execute(delete(Segment).where(Segment.recording_id == recording.id))
        for i, seg in enumerate(result["segments"]):
            session.add(
                Segment(
                    recording_id=recording.id,
                    idx=i,
                    start_sec=seg["start"],
                    end_sec=seg["end"],
                    speaker_key=seg.get("speaker"),
                    text=seg["text"],
                    words=seg.get("words") or None,
                )
            )
        recording.language = result.get("language")
        recording.stt_meta = result.get("meta") or {}
    except Exception as exc:
        session.rollback()
        recording.status = "error"
        recording.error = f"stt: {exc}"
        recording.progress = None
        recording.stage_started_at = None
        _log_stage(session, recording, "transcribe", started, status="error", error=str(exc))
        session.commit()
        raise

    recording.error = None
    recording.status = "enriching"
    recording.progress = None
    recording.stage_started_at = datetime.now(UTC)
    _log_stage(
        session, recording, "transcribe", started, status="done", meta=result.get("meta") or {}
    )
    session.commit()


def enrich_stage(session: Session, recording: Recording, enricher: Enricher | None) -> None:
    """엔리치먼트 실행. 실패해도 녹취록은 확보됐으므로 done으로 두고 에러만 기록한다.

    summary가 비어 있으면 다음 워커 시작 시 재큐잉돼 다시 시도된다.
    """
    started = datetime.now(UTC)
    if enricher is not None:
        try:
            enricher.enrich(session, recording)
        except Exception as exc:  # noqa: BLE001 - 요약 실패가 녹취록을 막지 않게
            session.rollback()
            logger.exception("엔리치먼트 실패: %s", recording.filename)
            recording.error = f"enrich: {exc}"
            _log_stage(session, recording, "enrich", started, status="error", error=str(exc))
        else:
            recording.error = None
            _log_stage(session, recording, "enrich", started, status="done")
    recording.status = "done"
    recording.progress = None
    recording.stage_started_at = None
    session.commit()


def process_one(
    session: Session,
    runner: RunnerClient,
    *,
    model: str | None = None,
    language: str | None = None,
    enricher: Enricher | None = None,
) -> bool:
    """다음 레코드 하나를 처리한다. 처리한 게 없으면 False."""
    recording = claim_next(session)
    if recording is None:
        return False
    logger.info("처리 시작: %s (%s)", recording.filename, recording.status)
    try:
        if recording.status != "enriching":
            transcribe_stage(session, recording, runner, model=model, language=language)
        enrich_stage(session, recording, enricher)
        logger.info("처리 완료: %s", recording.filename)
    except Exception:
        # 에러는 레코드에 격리 기록됐고, 워커는 다음 레코드로 계속
        logger.exception("처리 실패: %s", recording.filename)
        session.rollback()
    return True


def run_worker(
    session_factory: sessionmaker[Session],
    runner: RunnerClient,
    *,
    model: str | None = None,
    language: str | None = None,
    enricher: Enricher | None = None,
    idle_sleep_sec: float = 5.0,
) -> None:
    """워커 메인 루프. 중단(Ctrl-C)까지 큐를 소비한다."""
    with session_factory() as session:
        recovered = recover_in_flight(session)
        if recovered:
            logger.info("중단됐던 %d건을 재개 지점으로 되돌림", recovered)
        if enricher is not None:
            requeued = requeue_unenriched(session)
            if requeued:
                logger.info("요약 없는 완료 %d건을 엔리치먼트 재큐잉", requeued)
    while True:
        try:
            with session_factory() as session:
                worked = process_one(
                    session, runner, model=model, language=language, enricher=enricher
                )
            if not worked:
                time.sleep(idle_sleep_sec)
        except KeyboardInterrupt:
            logger.info("워커 종료")
            return
        except Exception:  # noqa: BLE001 - DB 단절 등 일시 장애에 워커가 죽지 않게
            logger.exception("워커 루프 오류 — %.0f초 후 재시도", idle_sleep_sec * 2)
            time.sleep(idle_sleep_sec * 2)
