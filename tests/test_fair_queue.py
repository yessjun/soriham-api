"""큐 공정성: 한 사람의 백로그가 나머지를 굶기지 않는다.

GPU가 하나라 워커의 집기가 유일한 직렬화 지점이고, 그래서 공정성을 넣을 자리도
거기뿐이다. 전역 최신순으로 집으면 1만 시간을 올린 사람 뒤에 전부가 선다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from soriham_api.models import Recording, Segment, Workspace
from soriham_api.worker import claim_next


def add(db, workspace, name: str, *, status: str = "pending", day: int = 1) -> Recording:
    recording = Recording(
        workspace_id=workspace.id,
        source="upload",
        path=f"/tmp/{workspace.slug}/{name}",
        filename=name,
        size_bytes=10,
        partial_hash=f"{workspace.slug}-{name}",
        recorded_at=datetime(2026, 8, day, tzinfo=UTC),
        status=status,
    )
    db.add(recording)
    db.commit()
    return recording


def test_긴_대기열이_다른_곳을_굶기지_않는다(db, workspace, other_workspace):
    """한쪽이 열 건, 다른 쪽이 한 건이어도 두 번째 집기는 다른 쪽으로 간다."""
    for i in range(10):
        add(db, workspace, f"많이-{i}.wav", day=i + 1)
    add(db, other_workspace, "하나.wav", day=1)

    first = claim_next(db)
    first.status = "transcribing"
    db.commit()
    second = claim_next(db)

    assert {first.workspace_id, second.workspace_id} == {workspace.id, other_workspace.id}


def test_한_번도_안_잡힌_곳이_먼저다(db, workspace, other_workspace):
    """새로 승인된 사람의 첫 녹음이 남의 백로그 뒤에 서면 안 된다."""
    workspace.last_claimed_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    add(db, workspace, "오래된곳.wav")
    add(db, other_workspace, "처음.wav")

    assert claim_next(db).workspace_id == other_workspace.id


def test_오래_안_잡힌_곳부터_돈다(db, workspace, other_workspace):
    now = datetime.now(UTC)
    workspace.last_claimed_at = now - timedelta(hours=2)
    other_workspace.last_claimed_at = now - timedelta(minutes=5)
    db.commit()
    add(db, workspace, "두시간.wav")
    add(db, other_workspace, "오분.wav")

    assert claim_next(db).workspace_id == workspace.id


def test_집으면_도장이_찍힌다(db, workspace, other_workspace):
    """도장을 안 찍으면 같은 곳이 매번 다시 1순위라 라운드로빈이 성립하지 않는다."""
    add(db, workspace, "a.wav")
    add(db, other_workspace, "b.wav")

    claimed = claim_next(db)
    db.commit()

    assert db.get(Workspace, claimed.workspace_id).last_claimed_at is not None


def test_워크스페이스가_하나면_워커_둘이_동시에_일한다(engine, db, workspace):
    """소유자 혼자 백로그를 도는 현실 워크로드다.

    나눌 대상이 없는데도 워크스페이스 행을 잠그면 워커 둘이 그 행에서 직렬화되고,
    수천 건이 대기 중인데 한쪽이 5초씩 잔다.
    """
    add(db, workspace, "첫째.wav", day=2)
    add(db, workspace, "둘째.wav", day=1)

    with Session(engine) as a, Session(engine) as b:
        first = claim_next(a)
        second = claim_next(b)

        assert first is not None
        assert second is not None
        assert first.id != second.id


def test_다른_워커가_쥔_워크스페이스는_건너뛴다(engine, db, workspace, other_workspace):
    """건너뛰지 않고 기다리면 워커가 남의 트랜잭션에 묶인다.

    잠금 대기 시간을 짧게 걸어 둔다. 안 걸면 이 보증이 깨졌을 때 테스트가 실패하는
    대신 멈춰 버려서, 무엇이 잘못됐는지 알려주지 않는다.
    """
    add(db, workspace, "남이쥔곳.wav")
    add(db, other_workspace, "빈곳아님.wav")
    workspace.last_claimed_at = None
    other_workspace.last_claimed_at = datetime.now(UTC)
    db.commit()

    with Session(engine) as a, Session(engine) as b:
        first = claim_next(a)  # 1순위인 workspace를 잡고 쥔 채로 둔다
        assert first.workspace_id == workspace.id
        b.execute(text("set local lock_timeout = '2s'"))
        second = claim_next(b)

        assert second is not None
        assert second.workspace_id == other_workspace.id


def test_잠글_녹음이_없는_워크스페이스에서_멈추지_않는다(engine, db, workspace, other_workspace):
    """1순위 워크스페이스의 마지막 녹음을 남이 쥐고 있으면 그 자리에서 멈출 수 있다.

    빈손인 워크스페이스를 목록에서 빼지 않으면 같은 곳을 계속 다시 골라 큐 전체가
    멈춘다. 다른 곳에 일감이 있는데도 아무도 일하지 않는 상태다.
    """
    workspace.last_claimed_at = None
    other_workspace.last_claimed_at = datetime.now(UTC)
    db.commit()
    only = add(db, workspace, "마지막하나.wav")
    add(db, other_workspace, "남은일감.wav")

    with Session(engine) as holder:
        # 1순위 워크스페이스의 유일한 녹음을 다른 트랜잭션이 행 잠금으로 쥔다.
        # 워크스페이스 행은 잠그지 않으므로 우리 쪽은 워크스페이스를 고르는 데 성공하고
        # 그 안에서 빈손이 된다 — 바로 그 분기다
        held = holder.scalars(
            select(Recording).where(Recording.id == only.id).with_for_update()
        ).one()
        assert held is not None

        with Session(engine) as worker:
            claimed = claim_next(worker)

            assert claimed is not None
            assert claimed.workspace_id == other_workspace.id


def test_워크스페이스_안에서는_전사가_엔리치먼트보다_먼저다(db, workspace):
    old = add(db, workspace, "요약만남음.wav", status="enriching", day=1)
    db.add(Segment(recording_id=old.id, idx=0, start_sec=0.0, end_sec=1.0, text="안녕"))
    add(db, workspace, "전사대기.wav", day=2)
    db.commit()

    assert claim_next(db).filename == "전사대기.wav"


def test_전사할_것이_없으면_엔리치먼트를_집는다(db, workspace):
    row = add(db, workspace, "요약만남음.wav", status="enriching")
    db.add(Segment(recording_id=row.id, idx=0, start_sec=0.0, end_sec=1.0, text="안녕"))
    db.commit()

    assert claim_next(db).filename == "요약만남음.wav"


def test_엔리치먼트만_남은_워크스페이스도_차례를_받는다(db, workspace, other_workspace):
    """후보를 전사 대기로만 추리면 요약만 남은 곳이 영원히 안 돈다."""
    row = add(db, other_workspace, "요약만.wav", status="enriching")
    db.add(Segment(recording_id=row.id, idx=0, start_sec=0.0, end_sec=1.0, text="안녕"))
    workspace.last_claimed_at = datetime.now(UTC)
    other_workspace.last_claimed_at = None
    db.commit()
    add(db, workspace, "전사대기.wav")

    assert claim_next(db).workspace_id == other_workspace.id


def test_일감이_없으면_아무것도_집지_않는다(db, workspace):
    assert claim_next(db) is None


# --- 처리 중인 녹음을 다시 집지 않는가 -------------------------------------------


def test_엔리치먼트_중인_녹음은_다른_워커가_집지_않는다(engine, db, workspace, other_workspace):
    """회전이 이 자리를 일상적으로 밟는다.

    전사가 끝난 녹음은 요약을 기다리는 것과 지금 처리 중인 것이 DB에서 구별돼야 한다.
    한 값으로 겸하면 워커가 붙어 있는 녹음을 다른 워커가 집어 LLM을 두 번 부르고,
    요약과 오류를 늦게 쓴 쪽이 덮는다.
    """
    from soriham_api.worker import transcribe_stage
    from test_worker import FakeRunnerClient

    add(db, workspace, "백로그.wav")
    friend = add(db, other_workspace, "친구회의.wav")
    now = datetime.now(UTC)
    # 친구 워크스페이스가 더 오래 안 잡혀서 회전이 그리로 민다
    workspace.last_claimed_at = now
    other_workspace.last_claimed_at = now - timedelta(minutes=1)
    db.commit()

    with Session(engine) as a:
        claimed = a.get(Recording, friend.id)
        transcribe_stage(a, claimed, FakeRunnerClient(), model=None, language=None)

        with Session(engine) as b:
            second = claim_next(b)

            assert second is not None
            assert second.id != friend.id


def test_요약을_시작하면_워크스페이스_잠금을_놓는다(engine, db, workspace, other_workspace):
    """LLM 호출이 끝날 때까지 잠그고 있으면 다른 워크스페이스의 전사가 멈춘다."""
    from soriham_api.worker import enrich_stage

    waiting = add(db, other_workspace, "요약대기.wav", status="enriching")
    db.add(Segment(recording_id=waiting.id, idx=0, start_sec=0.0, end_sec=1.0, text="안녕"))
    add(db, workspace, "전사대기.wav")
    other_workspace.last_claimed_at = None
    workspace.last_claimed_at = datetime.now(UTC)
    db.commit()

    도중에_집힌_것 = []

    class 도는동안_확인하는_엔리처:
        """요약이 끝난 뒤가 아니라 **도는 도중에** 본다.

        끝난 뒤에 보면 마지막 커밋이 이미 잠금을 놓은 상태라 무엇을 시험하는지 모른다.
        """

        def enrich(self, session, recording):
            with Session(engine) as b:
                b.execute(text("set local lock_timeout = '2s'"))
                도중에_집힌_것.append(claim_next(b))

    with Session(engine) as a:
        claimed = claim_next(a)
        assert claimed.id == waiting.id
        enrich_stage(a, claimed, 도는동안_확인하는_엔리처())

    assert len(도중에_집힌_것) == 1
    assert 도중에_집힌_것[0] is not None
    assert 도중에_집힌_것[0].filename == "전사대기.wav"


def test_요약이_실패해도_도장은_남는다(engine, db, workspace, other_workspace):
    """도장이 롤백에 쓸려 나가면 엔리처가 죽어 있는 동안 그 워크스페이스가 계속 1순위다."""
    from soriham_api.worker import process_one
    from test_worker import FakeRunnerClient

    class 죽은엔리처:
        def enrich(self, session, recording):
            raise RuntimeError("LLM down")

    waiting = add(db, other_workspace, "요약대기.wav", status="enriching")
    db.add(Segment(recording_id=waiting.id, idx=0, start_sec=0.0, end_sec=1.0, text="안녕"))
    add(db, workspace, "전사대기.wav")
    other_workspace.last_claimed_at = None
    workspace.last_claimed_at = datetime.now(UTC)
    db.commit()

    with Session(engine) as a:
        assert process_one(a, FakeRunnerClient(), enricher=죽은엔리처()) is True

    db.expire_all()
    assert db.get(Workspace, other_workspace.id).last_claimed_at is not None


def test_요약_중인_워커가_다른_워크스페이스를_붙잡고_있지_않는다(
    engine, db, workspace, other_workspace
):
    """빈손으로 지나간 워크스페이스의 잠금도 트랜잭션이 끝날 때까지 남는다.

    요약이 몇 분씩 걸리는데 그동안 잠금을 쥐고 있으면, 워커 둘이 워크스페이스 둘을
    도는 배치에서 나머지 하나가 후보를 전부 건너뛰고 빈손으로 잠든다. 다른 곳에
    일감이 있는데도 GPU가 논다.
    """
    from soriham_api.worker import enrich_stage

    now = datetime.now(UTC)
    # W2가 1순위지만 그 안의 유일한 녹음은 제3의 트랜잭션이 쥐고 있다
    held = add(db, other_workspace, "남이쥔것.wav")
    waiting = add(db, workspace, "요약대기.wav", status="enriching")
    db.add(Segment(recording_id=waiting.id, idx=0, start_sec=0.0, end_sec=1.0, text="안녕"))
    other_workspace.last_claimed_at = now - timedelta(minutes=10)
    workspace.last_claimed_at = now - timedelta(minutes=5)
    db.commit()

    도중에_집힌_것 = []

    class 도는동안_확인하는_엔리처:
        def enrich(self, session, recording):
            # 요약이 도는 사이 1순위 워크스페이스에 새 녹음이 들어온다.
            # **워크스페이스 행을 쥐고 있으면 이 INSERT부터 막힌다** — 외래 키가
            # 그 행에 키 공유 잠금을 걸기 때문이다. 업로드가 통째로 멈춘다는 뜻이라
            # 차례를 놓치는 것보다 훨씬 나쁘다
            with Session(engine) as writer:
                writer.execute(text("set local lock_timeout = '2s'"))
                ws = writer.get(Workspace, other_workspace.id)
                add(writer, ws, "새로들어온것.wav")
            with Session(engine) as b:
                b.execute(text("set local lock_timeout = '2s'"))
                도중에_집힌_것.append(claim_next(b))

    with Session(engine) as holder:
        holder.scalars(select(Recording).where(Recording.id == held.id).with_for_update()).one()

        with Session(engine) as a:
            claimed = claim_next(a)  # W2를 빈손으로 지나 W1의 요약 대기를 집는다
            assert claimed.id == waiting.id
            enrich_stage(a, claimed, 도는동안_확인하는_엔리처())

    assert 도중에_집힌_것[0] is not None
    assert 도중에_집힌_것[0].filename == "새로들어온것.wav"
