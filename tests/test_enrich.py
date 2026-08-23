from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from soriham_api import enrich as enrich_module
from soriham_api.enrich import (
    CHUNK_CHARS,
    LlmEnricher,
    OllamaGenerator,
    build_transcript,
    summarize,
)
from soriham_api.models import Recording, SpeakerName, Tag
from soriham_api.worker import process_one, requeue_unenriched
from test_worker import FakeRunnerClient, register

RESULT = {"title": "주간 회의", "summary": "핵심 요약입니다.", "tags": ["회의", "계획"]}


class FakeGenerator:
    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result or RESULT
        self.text_prompts: list[str] = []
        self.json_prompts: list[str] = []

    def generate_text(self, prompt: str) -> str:
        self.text_prompts.append(prompt)
        return f"부분 요약 {len(self.text_prompts)}"

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.json_prompts.append(prompt)
        return self.result


def test_summarize_short_transcript_single_call():
    gen = FakeGenerator()
    result = summarize(gen, "짧은 녹취록")
    assert result.title == "주간 회의"
    assert result.tags == ["회의", "계획"]
    assert gen.text_prompts == []
    assert "짧은 녹취록" in gen.json_prompts[0]


def test_summarize_long_transcript_chunks_then_combines():
    gen = FakeGenerator()
    summarize(gen, "가" * (CHUNK_CHARS * 2 + 100))
    assert len(gen.text_prompts) == 3  # 청크 3개 요약
    assert "부분 요약 1" in gen.json_prompts[0]
    assert "부분 요약 3" in gen.json_prompts[0]


def test_enricher_applies_title_summary_tags(db, tmp_path: Path, workspace):
    register(db, tmp_path, ["a.wav"], workspace)
    process_one(db, FakeRunnerClient(), enricher=LlmEnricher(FakeGenerator()))
    row = db.scalars(select(Recording)).one()
    assert row.status == "done"
    assert row.title == "주간 회의"
    assert row.summary == "핵심 요약입니다."
    assert sorted(t.name for t in row.tags) == ["계획", "회의"]
    # 태그 재사용: 같은 이름이 중복 생성되지 않는다
    assert db.scalars(select(Tag)).all().__len__() == 2


def test_enricher_keeps_user_title(db, tmp_path: Path, workspace):
    rows = register(db, tmp_path, ["a.wav"], workspace)
    rows[0].title = "사용자 지정 제목"
    db.commit()
    process_one(db, FakeRunnerClient(), enricher=LlmEnricher(FakeGenerator()))
    row = db.scalars(select(Recording)).one()
    assert row.title == "사용자 지정 제목"
    assert row.summary == "핵심 요약입니다."


def test_enrich_failure_still_marks_done(db, tmp_path: Path, workspace):
    class FailingEnricher:
        def enrich(self, session, recording):
            raise RuntimeError("모델 응답 없음")

    register(db, tmp_path, ["a.wav"], workspace)
    process_one(db, FakeRunnerClient(), enricher=FailingEnricher())
    row = db.scalars(select(Recording)).one()
    assert row.status == "done"
    assert row.summary is None
    assert "모델 응답 없음" in row.error


def test_requeue_unenriched_retries_failed_summaries(db, tmp_path: Path, workspace):
    register(db, tmp_path, ["a.wav"], workspace)

    class FailingEnricher:
        def enrich(self, session, recording):
            raise RuntimeError("일시 실패")

    process_one(db, FakeRunnerClient(), enricher=FailingEnricher())
    assert requeue_unenriched(db) == 1

    assert process_one(db, FakeRunnerClient(), enricher=LlmEnricher(FakeGenerator())) is True
    row = db.scalars(select(Recording)).one()
    assert row.status == "done"
    assert row.summary == "핵심 요약입니다."


def test_build_transcript_uses_speaker_names(db, tmp_path: Path, workspace):
    register(db, tmp_path, ["a.wav"], workspace)
    process_one(db, FakeRunnerClient())
    row = db.scalars(select(Recording)).one()
    db.add(SpeakerName(recording_id=row.id, speaker_key="SPEAKER_00", display_name="김소리"))
    db.commit()
    db.refresh(row)
    text = build_transcript(db, row)
    assert "김소리: 안녕하세요" in text
    assert "SPEAKER_01: 반갑습니다" in text


def test_ollama_generator_sends_schema_and_parses():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        content = json.dumps(RESULT) if "format" in body else "텍스트 요약"
        return httpx.Response(200, json={"message": {"content": content}})

    gen = OllamaGenerator(model="qwen3:8b", transport=httpx.MockTransport(handler))
    assert gen.generate_text("요약해") == "텍스트 요약"
    assert gen.generate_json("정리해", enrich_module.RESULT_SCHEMA) == RESULT
    assert seen[0]["model"] == "qwen3:8b"
    assert seen[0]["think"] is False
    assert seen[1]["format"]["required"] == ["title", "summary", "tags"]


def test_세그먼트가_없으면_되돌리지_않는다(db, tmp_path: Path, workspace):
    """전사 산출물이 없으면 요약할 거리가 없다. 되돌려 봐야 엔리치먼트가 헛돈다."""
    rows = register(db, tmp_path, ["a.wav"], workspace)
    rows[0].status = "done"
    rows[0].summary = None
    db.commit()

    assert requeue_unenriched(db) == 0
    db.refresh(rows[0])
    assert rows[0].status == "done"
