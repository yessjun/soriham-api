from __future__ import annotations

import uuid

from soriham_api.models import RECORDING_STATUSES, Base


def test_expected_tables_defined():
    assert set(Base.metadata.tables) == {
        "users",
        "workspaces",
        "workspace_members",
        "user_sessions",
        "invites",
        "recording_shares",
        "share_links",
        "auth_attempts",
        "recordings",
        "segments",
        "speaker_names",
        "tags",
        "recording_tags",
        "job_log",
    }


def test_state_machine_statuses():
    assert RECORDING_STATUSES == (
        "pending",
        "transcribing",
        "diarizing",
        "enriching",
        "done",
        "error",
        "missing",
        "duplicate",
        "quota_blocked",
    )


def test_public_id_columns_are_uuid_with_server_default():
    for table in (
        "recordings",
        "tags",
        "users",
        "workspaces",
        "user_sessions",
        "invites",
        "recording_shares",
        "share_links",
    ):
        col = Base.metadata.tables[table].columns["public_id"]
        assert col.server_default is not None
        assert col.unique
        assert col.type.python_type is uuid.UUID


def test_internal_pk_is_bigint_identity():
    for table in ("recordings", "segments", "tags", "job_log"):
        pk = Base.metadata.tables[table].columns["id"]
        assert pk.primary_key
        assert pk.identity is not None


def test_태그_이름은_워크스페이스_안에서만_유일하다():
    """전역 유일이면 한 곳이 만든 이름을 다른 곳이 그대로 물려받는다."""
    constraints = {
        c.name: tuple(col.name for col in c.columns)
        for c in Base.metadata.tables["tags"].constraints
        if c.name is not None
    }
    assert constraints.get("uq_tags_workspace_name") == ("workspace_id", "name")


def test_사용_이력은_녹음_삭제를_살아남는다():
    """job_log가 녹음을 따라 지워지면 한도를 공짜로 되돌릴 수 있다."""
    job_log = Base.metadata.tables["job_log"]
    assert job_log.columns["recording_id"].nullable
    (fk,) = job_log.columns["recording_id"].foreign_keys
    assert fk.ondelete == "SET NULL"
    assert not job_log.columns["workspace_id"].nullable
