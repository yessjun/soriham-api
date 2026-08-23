"""엔리치먼트: 녹취록에서 제목·요약·태그 생성.

Ollama 로컬 LLM이 기본, ANTHROPIC_API_KEY가 있으면 Claude API 어댑터 선택 가능.
긴 녹취록은 청크 요약 후 통합한다.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy.orm import Session

from soriham_api.models import Recording
from soriham_api.tenancy import resolve_tag

logger = logging.getLogger(__name__)

# 이 길이(문자)를 넘는 녹취록은 청크로 나눠 요약 후 통합
CHUNK_CHARS = 24_000

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "녹음 내용을 대표하는 짧은 한국어 제목"},
        "summary": {"type": "string", "description": "핵심 내용의 한국어 요약 (3~6문장)"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "주제 분류 태그 1~5개 (한국어 명사형)",
        },
    },
    "required": ["title", "summary", "tags"],
    "additionalProperties": False,
}

ENRICH_PROMPT = """다음은 음성 녹음의 녹취록이다. 내용을 파악해 JSON으로 답하라.
- title: 내용을 대표하는 짧은 제목 (파일명이 아니라 내용 기준)
- summary: 핵심 논의와 결론 중심의 요약 3~6문장
- tags: 주제 분류 태그 1~5개 (한국어 명사형, 짧게)

녹취록:
{transcript}"""

CHUNK_PROMPT = """다음은 긴 음성 녹음 녹취록의 일부({index}/{total})다.
이 부분의 핵심 내용을 한국어 3~5문장으로 요약만 하라. 다른 말은 붙이지 마라.

녹취록 일부:
{chunk}"""

COMBINE_PROMPT = """다음은 긴 음성 녹음을 부분별로 요약한 것이다. 전체 내용을 파악해
JSON으로 답하라.
- title: 내용을 대표하는 짧은 제목
- summary: 전체를 아우르는 요약 3~6문장
- tags: 주제 분류 태그 1~5개 (한국어 명사형, 짧게)

부분 요약:
{transcript}"""


@dataclass
class EnrichResult:
    title: str
    summary: str
    tags: list[str]


class Generator(Protocol):
    """LLM 백엔드 계약: 자유 텍스트 생성과 스키마 강제 JSON 생성."""

    def generate_text(self, prompt: str) -> str: ...

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]: ...


def build_transcript(session: Session, recording: Recording) -> str:
    """세그먼트를 화자 표시와 함께 녹취록 텍스트로 합친다."""
    names = {n.speaker_key: n.display_name for n in recording.speaker_names}
    lines = []
    for seg in recording.segments:
        if seg.kind != "speech":  # 소음 구간은 요약에 넣지 않는다
            continue
        speaker = names.get(seg.speaker_key, seg.speaker_key) if seg.speaker_key else None
        lines.append(f"{speaker}: {seg.text}" if speaker else seg.text)
    return "\n".join(lines)


def summarize(
    generator: Generator, transcript: str, *, on_step: Callable[[], None] | None = None
) -> EnrichResult:
    """녹취록 하나를 (필요 시 청크 요약을 거쳐) 제목·요약·태그로 만든다.

    `on_step`은 청크 하나가 끝날 때마다 불린다. 긴 회의는 청크마다 LLM을 부르느라
    몇 분씩 걸리는데, 그동안 아무 기록도 안 남으면 옆 워커가 죽은 작업으로 본다.
    """
    if len(transcript) > CHUNK_CHARS:
        chunks = [transcript[i : i + CHUNK_CHARS] for i in range(0, len(transcript), CHUNK_CHARS)]
        partials = []
        for i, chunk in enumerate(chunks):
            partials.append(
                generator.generate_text(
                    CHUNK_PROMPT.format(index=i + 1, total=len(chunks), chunk=chunk)
                )
            )
            if on_step is not None:
                on_step()
        prompt = COMBINE_PROMPT.format(transcript="\n\n".join(partials))
    else:
        prompt = ENRICH_PROMPT.format(transcript=transcript)

    data = generator.generate_json(prompt, RESULT_SCHEMA)
    return EnrichResult(
        title=str(data["title"]).strip(),
        summary=str(data["summary"]).strip(),
        tags=[str(t).strip() for t in data["tags"] if str(t).strip()][:5],
    )


class LlmEnricher:
    """워커의 Enricher 구현: 생성 결과를 레코드에 반영한다."""

    def __init__(self, generator: Generator) -> None:
        self._generator = generator

    def enrich(
        self, session: Session, recording: Recording, *, on_step: Callable[[], None] | None = None
    ) -> None:
        transcript = build_transcript(session, recording)
        if not transcript.strip():
            logger.info("녹취록이 비어 있어 엔리치먼트 생략: %s", recording.filename)
            recording.summary = ""  # 재큐잉 대상에서 제외(요약 불가 확정)
            return
        result = summarize(self._generator, transcript, on_step=on_step)

        if recording.title is None and result.title:
            recording.title = result.title
        recording.summary = result.summary or recording.summary
        for name in result.tags:
            tag = resolve_tag(session, recording.workspace_id, name)
            if tag not in recording.tags:
                recording.tags.append(tag)


class OllamaGenerator:
    """Ollama /api/chat 백엔드 (structured outputs의 format 필드로 스키마 강제)."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen3:8b",
        timeout_sec: float = 600.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.timeout_sec = timeout_sec
        self.transport = transport

    def _chat(self, prompt: str, format_schema: dict[str, Any] | None) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            # 요약 작업에는 추론 모드가 과해 응답만 받는다
            "think": False,
        }
        if format_schema is not None:
            body["format"] = format_schema
        kwargs: dict[str, Any] = {"base_url": self.base_url, "timeout": self.timeout_sec}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        with httpx.Client(**kwargs) as client:
            resp = client.post("/api/chat", json=body)
            resp.raise_for_status()
            return resp.json()["message"]["content"]

    def generate_text(self, prompt: str) -> str:
        return self._chat(prompt, None).strip()

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        return json.loads(self._chat(prompt, schema))


class ClaudeGenerator:
    """Claude API 백엔드 (공식 SDK, 구조화 출력). anthropic 패키지는 claude extra."""

    def __init__(self, model: str = "claude-opus-5") -> None:
        self.model = model
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def _create(self, prompt: str, **kwargs: Any) -> Any:
        response = self._get_client().messages.create(
            model=self.model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude가 요청을 거부함 (stop_reason: refusal)")
        return response

    def generate_text(self, prompt: str) -> str:
        response = self._create(prompt)
        return "".join(b.text for b in response.content if b.type == "text").strip()

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        response = self._create(
            prompt, output_config={"format": {"type": "json_schema", "schema": schema}}
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        return json.loads(text)


def build_enricher(backend: str, *, ollama_url: str, ollama_model: str) -> LlmEnricher | None:
    """설정에 따른 Enricher. off면 None(엔리치먼트 생략)."""
    if backend == "off":
        return None
    if backend == "claude":
        return LlmEnricher(ClaudeGenerator())
    return LlmEnricher(OllamaGenerator(base_url=ollama_url, model=ollama_model))
