"""처리 워커: 큐 소비 → stt 러너 호출 → 세그먼트 저장 → 엔리치먼트 → done.

체크포인트 원칙: 단계 산출물(세그먼트, 요약)이 DB에 남으므로 강제 종료 후
재시작해도 남은 단계부터 이어서 처리한다.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from sqlalchemy import case, delete, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from soriham_api.auth import sweep_sessions
from soriham_api.ingest import resume_status
from soriham_api.models import JobLog, Recording, Segment, Workspace
from soriham_api.quota import allows_transcription
from soriham_api.ratelimit import sweep as sweep_attempts
from soriham_api.stt_client import RunnerClient, RunnerUnavailable

logger = logging.getLogger(__name__)

# 화자분리 실패는 녹취록을 막지 않되 화면에 남긴다 — 접두사로 엔리치먼트 에러와 구분한다
DIARIZE_ERROR_PREFIX = "화자분리 실패: "

# 워커가 잡을 수 있는 대기 상태와, 재시작 시 재개 대상인 진행 중 상태
CLAIMABLE = ("pending",)
# 요약을 기다리는 상태. 처리 중(summarizing)과 갈라 둔다
ENRICH_WAITING = "enriching"
# 집는 순서. 전사 대기가 먼저고 요약 대기가 그다음이다
CLAIM_TIERS = (CLAIMABLE, (ENRICH_WAITING,))
# 후보를 추리는 쪽과 실제로 집는 쪽이 목록을 따로 들면 반드시 어긋난다. 어긋나면
# 워크스페이스가 후보에 못 들어와 그 안의 일감이 영원히 안 돈다
CLAIMABLE_STATUSES = tuple(s for tier in CLAIM_TIERS for s in tier)
IN_FLIGHT = ("transcribing", "diarizing", "summarizing")
# 이 시간보다 오래 아무 기록이 없으면 죽은 워커가 남긴 것으로 본다. 화자분리처럼
# 진행률을 못 내는 단계가 있어 하트비트를 따로 찍는다
STALE_AFTER = timedelta(minutes=5)
HEARTBEAT_EVERY = timedelta(seconds=30)
# 바쁠 때도 정리가 돌게 한다. 유휴에만 걸어 두면 백로그를 도는 몇 주 동안 한 번도
# 안 돌아 한도 해제가 멈춘다
MAINTENANCE_EVERY = 20


class Enricher(Protocol):
    """제목·요약·태그 생성기. 엔리치먼트 단계에서 구현체가 붙는다."""

    def enrich(self, session: Session, recording: Recording) -> None: ...


def recover_in_flight(session: Session, *, now: datetime | None = None) -> int:
    """중단된 채 남은 레코드를 체크포인트 기준으로 되돌린다.

    최근에 기록이 남은 것은 건드리지 않는다. 무조건 되돌리면 워커를 하나 재시작할 때
    옆 워커가 지금 붙어 있는 녹음을 빼앗아 같은 오디오를 두 번 전사한다.
    """
    now = now or datetime.now(UTC)
    count = 0
    stale = select(Recording).where(
        Recording.status.in_(IN_FLIGHT),
        Recording.updated_at < now - STALE_AFTER,
    )
    for recording in session.scalars(stale):
        # 세그먼트까지 저장돼 있으면 엔리치먼트 대기로 돌아간다
        recording.status = resume_status(recording)
        count += 1
        logger.info("재개: %s -> %s", recording.filename, recording.status)
    session.commit()
    return count


def retry_failed(session: Session, *, workspace_id: int | None = None) -> int:
    """실패한 녹음을 재개 지점으로 되돌린다.

    error에서 나가는 길이 삭제뿐이면, 러너가 잠깐 죽은 사이 실패한 수천 건을 손으로
    지우고 다시 올려야 한다. 세그먼트가 남아 있으면 전사를 건너뛴다.
    """
    # 재개 지점 판정은 resume_status와 같다. 파이썬으로 돌면 행마다 세그먼트를 통째로
    # 읽어 오는데, 러너가 몇 시간 죽어 있던 뒤라면 그 대상이 수천 건이다
    has_segments = exists().where(Segment.recording_id == Recording.id)
    stmt = update(Recording).where(Recording.status == "error")
    if workspace_id is not None:
        stmt = stmt.where(Recording.workspace_id == workspace_id)
    result = session.execute(
        stmt.values(
            status=case(
                (Recording.summary.is_not(None), "done"),
                (has_segments, ENRICH_WAITING),
                else_="pending",
            ),
            error=None,
        )
    )
    session.commit()
    return result.rowcount


def requeue_unenriched(session: Session) -> int:
    """요약 없이 done인 레코드를 엔리치먼트 대상으로 되돌린다(백필·재시도).

    세그먼트가 있는지만 알면 되므로 SQL로 묻는다. 파이썬으로 돌면 행마다 세그먼트를
    통째로 읽어 오는데, 백필 대상은 백로그 전체일 수 있다.
    """
    result = session.execute(
        update(Recording)
        .where(
            Recording.status == "done",
            Recording.summary.is_(None),
            exists().where(Segment.recording_id == Recording.id),
        )
        .values(status=ENRICH_WAITING)
    )
    session.commit()
    return result.rowcount


def idle_maintenance(session_factory: sessionmaker[Session]) -> None:
    """할 일이 없을 때 도는 정리.

    한도 해제를 기동 시에만 보면 데몬이 재시작될 때까지 풀리지 않는다. 30일 창이
    지나도, 관리자가 한도를 올려도 그대로 서 있는다.

    죽은 세션과 지난 시도 기록도 여기서 치운다. 웹 프로세스에 얹으면 요청 하나가
    느려지고, 별도 스케줄러를 두면 배포에 프로세스가 하나 더 는다. 워커는 어차피
    상시 돌고 유휴 시간이 있다.
    """
    with session_factory() as session:
        released = release_quota_blocked(session)
        swept = sweep_sessions(session)
        attempts = sweep_attempts(session)
        if swept or attempts:
            session.commit()
    if released:
        logger.info("한도가 풀려 %d건을 큐로 되돌림", released)
    if swept or attempts:
        logger.info("정리: 죽은 세션 %d건, 지난 시도 기록 %d건", swept, attempts)


def release_quota_blocked(session: Session) -> int:
    """한도에 걸려 세워둔 녹음 중 지금은 통과하는 것을 큐로 되돌린다."""
    blocked = session.scalars(select(Recording).where(Recording.status == "quota_blocked")).all()
    released = 0
    for recording in blocked:
        if allows_transcription(session, recording):
            # pending으로 일괄 되돌리면 이미 전사된 녹음이 다시 전사돼 같은 오디오가
            # 한도를 두 번 깎는다. 저장된 산출물이 있으면 그 지점부터 이어야 한다
            recording.status = resume_status(recording)
            released += 1
    if released:
        session.commit()
    return released


def claim_next(session: Session) -> Recording | None:
    """다음 일감 하나. 워크스페이스를 돌아가며 고르고, 그 안에서는 최신 녹음이 먼저다.

    전역 최신순으로 집으면 한 사람이 백로그 1만 시간을 올리는 순간 나머지 전부가 그
    뒤에 선다. GPU가 하나라 여기가 유일한 직렬화 지점이므로, 공정성을 넣을 자리도
    여기뿐이다.

    잠금 순서는 항상 workspaces 다음 recordings다. 뒤집으면 데드락이 나고, 부하가
    걸려야 드러난다.
    """
    eligible = _workspaces_with_work(session)
    if not eligible:
        return None
    if len(eligible) == 1:
        # 워크스페이스가 하나뿐이면 나눌 것이 없다. 그런데도 워크스페이스 행을 잠그면
        # 워커 둘이 그 행에서 직렬화돼, 수천 건이 대기 중인데 한쪽이 5초씩 잔다.
        # 소유자 혼자 백로그를 도는 현실 워크로드가 정확히 이 모양이다
        return _claim_in_workspace(session, eligible[0])

    # 이 호출이 찍는 도장은 전부 같은 시각이다. 반복마다 시계를 읽을 이유가 없다
    now = datetime.now(UTC)
    for _ in range(len(eligible)):
        workspace = session.scalars(
            select(Workspace)
            .where(Workspace.id.in_(eligible))
            # 한 번도 안 잡힌 곳이 맨 앞이다. 새로 승인된 사람의 첫 녹음이 바로 돈다.
            # READ COMMITTED에서는 이 정렬이 재평가되지 않아 가장 오래된 곳을 놓칠 수
            # 있다 — 공정성이 흔들릴 뿐 잘못된 결과는 아니다
            .order_by(Workspace.last_claimed_at.asc().nulls_first(), Workspace.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        ).first()
        if workspace is None:
            # 남은 워크스페이스를 전부 다른 워커가 쥐고 있다
            return None
        # 빈손이어도 시각을 기록한다. 안 하면 그 워크스페이스가 매번 다시 1순위가 돼서
        # 다른 곳에 일감이 있는데도 큐가 멈춘다
        workspace.last_claimed_at = now
        recording = _claim_in_workspace(session, workspace.id)
        if recording is not None:
            return recording
    return None


def _workspaces_with_work(session: Session) -> list[int]:
    """일감이 남은 워크스페이스. 잠그지 않고 후보만 추린다."""
    return list(
        session.scalars(
            select(Recording.workspace_id)
            .where(Recording.status.in_(CLAIMABLE_STATUSES))
            .distinct()
        ).all()
    )


def _claim_in_workspace(session: Session, workspace_id: int) -> Recording | None:
    """한 워크스페이스 안에서 집는다. 전사 대기가 먼저, 그다음이 엔리치먼트 재개분."""
    for statuses in CLAIM_TIERS:
        recording = session.scalars(
            select(Recording)
            .where(
                Recording.workspace_id == workspace_id,
                Recording.status.in_(statuses),
            )
            .order_by(Recording.recorded_at.desc().nulls_last(), Recording.id.desc())
            .with_for_update(skip_locked=True)
            .limit(1)
        ).first()
        if recording is not None:
            return recording
    return None


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
    """처리 이력을 남긴다. 호출자 트랜잭션이 아니라 별도 세션에서 커밋한다.

    사용량 집계의 근거라 작업이 실패해도 남아야 한다. 같은 트랜잭션에 넣으면 전사 중에
    녹음이 삭제될 때 롤백으로 이력까지 날아가고, 그러면 전사 시간 한도를 우회할 수 있다.
    """
    meta = meta or {}
    row = dict(
        # 녹음이 삭제돼도 집계에 남도록 workspace_id를 따로 저장한다
        workspace_id=recording.workspace_id,
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
    with Session(bind=session.get_bind()) as ledger:
        try:
            ledger.add(JobLog(**row))
            ledger.commit()
        except IntegrityError:
            # 전사 중에 녹음이 삭제된 경우. FK 위반이므로 recording_id 없이 다시 저장한다
            ledger.rollback()
            ledger.add(JobLog(**{**row, "recording_id": None}))
            ledger.commit()


def _beat(session: Session, recording: Recording, now: datetime, last: datetime) -> bool:
    """살아 있다는 표시를 남긴다. 안 남기면 옆 워커가 죽은 작업으로 보고 가져간다.

    화자분리처럼 진행률을 못 내는 단계가 길면 그 사이에 아무 기록도 안 남는다.
    """
    if now - last < HEARTBEAT_EVERY:
        return False
    recording.updated_at = now
    session.commit()
    return True


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

    last_beat = started

    def report(stage: str | None, ratio: float | None) -> None:
        nonlocal last_beat
        now = datetime.now(UTC)
        if stage is not None and stage != "transcribe":
            # 화자분리처럼 비율을 낼 수 없는 단계로 넘어갔다. 마지막 값을 그대로 두면
            # 그 퍼센트에 멈춘 것처럼 보이므로 비우고 경과 시간도 다시 잡는다
            if recording.progress is not None:
                recording.progress = None
                recording.stage_started_at = now
                last_beat = now
                session.commit()
            else:
                if _beat(session, recording, now, last_beat):
                    last_beat = now
            return
        # 3초 폴링마다 쓰지 않는다. 1%p 이상 움직였을 때만
        moved = ratio is not None and (
            recording.progress is None or abs(ratio - recording.progress) >= 0.01
        )
        if not moved:
            if _beat(session, recording, now, last_beat):
                last_beat = now
            return
        last_beat = now
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
                    kind=seg.get("kind") or "speech",
                )
            )
        recording.language = result.get("language")
        recording.stt_meta = result.get("meta") or {}
    except RunnerUnavailable as exc:
        # 러너에 못 닿은 것이지 이 파일의 문제가 아니다. error로 굳히면 러너가 죽어 있는
        # 동안 대기열 전체가 error가 되고, 되돌릴 길은 사람 손뿐이다
        session.rollback()
        recording.status = "pending"
        recording.progress = None
        recording.stage_started_at = None
        session.commit()
        logger.warning("러너에 닿지 못함, 큐에 되돌림: %s (%s)", recording.filename, exc)
        raise
    except Exception as exc:
        session.rollback()
        recording.status = "error"
        recording.error = f"stt: {exc}"
        recording.progress = None
        recording.stage_started_at = None
        _log_stage(session, recording, "transcribe", started, status="error", error=str(exc))
        session.commit()
        raise

    # 화자분리가 실패해도 녹취록은 살아 있으므로 진행은 시키되, 조용히 넘기지 않는다
    meta = recording.stt_meta or {}
    recording.error = (
        f"{DIARIZE_ERROR_PREFIX}{meta['diarize_error']}" if meta.get("diarize_error") else None
    )
    # 곧바로 처리 중 표식을 단다. 여기서 "요약 대기"로 커밋하면 잠금이 풀린 그 틈에
    # 다른 워커가 같은 녹음을 집는다
    recording.status = "summarizing"
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
    # 일을 시작하기 전에 처리 중임을 커밋한다. 전사 단계의 transcribing과 같은 역할이다.
    # 이 커밋이 워크스페이스 행 잠금을 놓고 순환 기록도 확정한다. 뒤에 두면 엔리처가
    # 죽을 때 롤백에 함께 사라져 그 워크스페이스가 계속 1순위로 남는다
    if recording.status != "summarizing":
        recording.status = "summarizing"
        recording.stage_started_at = started
    session.commit()
    # 전사 단계가 남긴 경고는 엔리치먼트가 성공해도 지우지 않는다
    carried = recording.error if (recording.error or "").startswith(DIARIZE_ERROR_PREFIX) else None
    if enricher is not None:
        try:
            enricher.enrich(session, recording)
        except Exception as exc:  # noqa: BLE001 - 요약 실패가 녹취록을 막지 않게
            session.rollback()
            logger.exception("엔리치먼트 실패: %s", recording.filename)
            recording.error = f"enrich: {exc}"
            _log_stage(session, recording, "enrich", started, status="error", error=str(exc))
        else:
            recording.error = carried
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
    # 로그에 쓸 이름을 미리 붙잡고 롤백을 로깅보다 먼저 한다. 처리 중에 녹음이
    # 지워지면 세션이 롤백 대기 상태가 되는데, 그때 아직 적재되지 않은 속성을 읽으면
    # 예외가 격리를 뚫고 나간다. 지금 배치에서는 속성이 이미 적재돼 있어 닿지 않지만,
    # 그 사정에 기대고 싶지 않은 자리다
    name = recording.filename
    # 한도가 사는 자원은 전사 시간이다. 엔리치먼트만 남은 녹음은 GPU 비용이 이미
    # 치러졌으니 여기서 막으면 요약만 영영 안 붙는다
    if recording.status != ENRICH_WAITING and not allows_transcription(session, recording):
        # 고장이 아니라 풀리는 상태다. 기간이 지나거나 한도가 오르면 되돌아온다
        recording.status = "quota_blocked"
        session.commit()
        logger.info("사용량 한도로 보류: %s", name)
        return True
    logger.info("처리 시작: %s (%s)", name, recording.status)
    try:
        if recording.status != ENRICH_WAITING:
            transcribe_stage(session, recording, runner, model=model, language=language)
        enrich_stage(session, recording, enricher)
        logger.info("처리 완료: %s", name)
    except RunnerUnavailable:
        # 다음 녹음을 바로 집으면 대기열을 통째로 훑으며 같은 실패를 반복한다
        session.rollback()
        raise
    except Exception:
        # 에러는 레코드에 격리 기록됐고, 워커는 다음 레코드로 계속
        session.rollback()
        logger.exception("처리 실패: %s", name)
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
    since_maintenance = 0
    while True:
        try:
            with session_factory() as session:
                worked = process_one(
                    session, runner, model=model, language=language, enricher=enricher
                )
            since_maintenance += 1
            if not worked or since_maintenance >= MAINTENANCE_EVERY:
                since_maintenance = 0
                idle_maintenance(session_factory)
            if not worked:
                time.sleep(idle_sleep_sec)
        except RunnerUnavailable as exc:
            # 러너가 돌아올 때까지 물러선다. 바로 다음 녹음을 집으면 대기열을 훑으며
            # 같은 실패를 반복한다
            logger.warning("러너를 기다리는 중 — %.0f초 후 재시도 (%s)", idle_sleep_sec * 4, exc)
            time.sleep(idle_sleep_sec * 4)
        except KeyboardInterrupt:
            logger.info("워커 종료")
            return
        except Exception:  # noqa: BLE001 - DB 단절 등 일시 장애에 워커가 죽지 않게
            logger.exception("워커 루프 오류 — %.0f초 후 재시도", idle_sleep_sec * 2)
            time.sleep(idle_sleep_sec * 2)
