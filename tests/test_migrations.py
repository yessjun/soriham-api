"""마이그레이션이 만든 실제 스키마와 모델 선언이 어긋나지 않는지 본다.

`compare_metadata`가 테이블·컬럼·타입·FK·인덱스(표현식과 정렬 방향 포함)를 덮지만
**보지 않는 것이 셋 있고**, 셋 다 하필 이 스키마의 급소라 검사를 따로 둔다:

- **CHECK 제약** — 상태머신 값이 여기 산다. 모델에 상태를 추가하고 마이그레이션을
  빠뜨리면 그 상태를 쓰는 순간까지 아무도 모른다.
- **인덱스 접근 방식과 연산자 클래스** — 한국어 부분 문자열 검색은 `gin` + `gin_trgm_ops`에
  전적으로 기대는데, 이게 조용히 btree로 바뀌어도 `compare_metadata`는 차이를 내지 않는다.
  검색이 느려질 뿐 결과는 나오므로 테스트도 화면도 멀쩡해 보인다.
- **부분 인덱스의 WHERE 술어** — 술어가 사라져도 차이를 내지 않는다. 워크스페이스마다
  소유자가 하나임을 강제하는 것이 이 술어라, 없어지면 강제가 풀리거나(술어만 빠지면)
  구성원이 워크스페이스당 한 명으로 잘못 잠긴다.
"""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import BigInteger, Column, MetaData, Table, text

from soriham_api.models import RECORDING_STATUSES, Base


def test_마이그레이션과_모델_선언이_일치한다(engine):
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"모델과 마이그레이션이 갈라졌다: {diff}"


TRIGRAM_INDEXES = (
    "ix_segments_text_trgm",
    "ix_recordings_filename_trgm",
    "ix_recordings_title_trgm",
    "ix_recordings_summary_trgm",
)


def test_한국어_검색_인덱스가_trigram_gin이다(engine):
    """`compare_metadata`가 못 보는 자리. btree로 바뀌어도 검색 결과는 나오므로
    테스트도 화면도 멀쩡해 보이고, 느려진 것만 남는다."""
    with engine.connect() as conn:
        rows = dict(
            conn.execute(
                text("select indexname, indexdef from pg_indexes where indexname = any(:names)"),
                {"names": list(TRIGRAM_INDEXES)},
            ).all()
        )
    for name in TRIGRAM_INDEXES:
        definition = rows.get(name)
        assert definition is not None, f"{name} 인덱스가 DB에 없다"
        assert "USING gin" in definition, f"{name}이 gin이 아니다: {definition}"
        assert "gin_trgm_ops" in definition, f"{name}에 trigram 연산자 클래스가 없다: {definition}"


# (인덱스 이름, WHERE 술어에 반드시 들어가야 하는 조각)
PARTIAL_INDEX_PREDICATES = (
    ("uq_workspace_members_single_owner", "role = 'owner'"),
    ("uq_recording_shares_user", "user_id IS NOT NULL"),
    ("uq_recording_shares_email", "invite_email IS NOT NULL"),
)


def test_부분_인덱스의_술어가_살아있다(engine):
    """`compare_metadata`가 못 보는 자리. 술어가 사라져도 인덱스는 남아 있어서
    스키마 비교로는 멀쩡해 보이고, 강제하던 규칙만 조용히 바뀐다."""
    with engine.connect() as conn:
        rows = dict(
            conn.execute(
                text("select indexname, indexdef from pg_indexes where indexname = any(:names)"),
                {"names": [name for name, _ in PARTIAL_INDEX_PREDICATES]},
            ).all()
        )
    for name, predicate in PARTIAL_INDEX_PREDICATES:
        definition = rows.get(name)
        assert definition is not None, f"{name} 인덱스가 DB에 없다"
        assert "WHERE" in definition.upper(), f"{name}이 부분 인덱스가 아니다: {definition}"
        assert predicate in definition, f"{name}의 술어가 다르다: {definition}"


def test_드리프트_검사가_실제로_차이를_잡는다(engine):
    """위 검사가 공허하게 통과하지 않음을 보인다.

    통과 이유가 "차이가 없어서"가 아니라 "검사가 죽어서"인 경우를 가려낸다 — 검사
    자체가 조용히 무력해지면 그 뒤로 어떤 드리프트도 영원히 안 보인다.
    """
    probe = MetaData()
    Table("드리프트_탐침", probe, Column("id", BigInteger, primary_key=True))
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), probe)
    added = [d for d in diff if d[0] == "add_table" and d[1].name == "드리프트_탐침"]
    assert added, f"DB에 없는 테이블을 드리프트로 잡지 못했다: {diff}"


def test_상태머신_CHECK_제약이_모델과_같다(engine):
    """`compare_metadata`가 못 보는 자리. 상태를 추가하고 마이그레이션을 빠뜨리면
    새 상태를 쓰는 순간 런타임에 IntegrityError로만 드러난다."""
    with engine.connect() as conn:
        definition = conn.scalar(
            text(
                "select pg_get_constraintdef(oid) from pg_constraint "
                "where conname = 'recordings_status_check'"
            )
        )
    assert definition is not None, "recordings_status_check 제약이 DB에 없다"
    for status in RECORDING_STATUSES:
        assert f"'{status}'" in definition, f"DB CHECK에 {status} 없음: {definition}"
