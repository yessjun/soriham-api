from __future__ import annotations

import uuid

from soriham_api.models import RECORDING_STATUSES, Base


def test_expected_tables_defined():
    assert set(Base.metadata.tables) == {
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
    )


def test_public_id_columns_are_uuid_with_server_default():
    for table in ("recordings", "tags"):
        col = Base.metadata.tables[table].columns["public_id"]
        assert col.server_default is not None
        assert col.unique
        assert col.type.python_type is uuid.UUID


def test_internal_pk_is_bigint_identity():
    for table in ("recordings", "segments", "tags", "job_log"):
        pk = Base.metadata.tables[table].columns["id"]
        assert pk.primary_key
        assert pk.identity is not None
