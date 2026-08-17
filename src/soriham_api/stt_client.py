"""stt 러너 HTTP 잡 API 어댑터. 러너 URL만 알면 로컬·원격 어디든 같은 계약."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class RunnerJobLost(Exception):
    """러너가 잡을 잊었다(재시작 등) — 재제출 대상."""


class RunnerJobFailed(Exception):
    """러너가 잡을 error로 끝냈다."""


@dataclass
class RunnerClient:
    """러너 호출 클라이언트.

    upload=False면 경로를 그대로 전달한다(러너와 파일시스템 공유 전제,
    러너 쪽 STT_SHARED_DIR 필요). True면 오디오를 multipart로 업로드한다.
    """

    base_url: str
    upload: bool = False
    poll_interval_sec: float = 3.0
    timeout_sec: float = 30.0
    transport: httpx.BaseTransport | None = None

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url, timeout=self.timeout_sec, transport=self.transport
        )

    def health(self) -> dict[str, Any]:
        with self._client() as client:
            resp = client.get("/health")
            resp.raise_for_status()
            return resp.json()

    def submit(
        self, audio_path: Path, *, model: str | None, language: str | None, diarize: bool
    ) -> str:
        data: dict[str, Any] = {"diarize": "true" if diarize else "false"}
        if model:
            data["model"] = model
        if language:
            data["language"] = language
        with self._client() as client:
            if self.upload:
                with audio_path.open("rb") as f:
                    resp = client.post(
                        "/jobs", data=data, files={"file": (audio_path.name, f)}, timeout=None
                    )
            else:
                resp = client.post("/jobs", data={**data, "path": str(audio_path)})
            resp.raise_for_status()
            return resp.json()["job_id"]

    def wait(self, job_id: str) -> dict[str, Any]:
        """잡이 끝날 때까지 폴링한다. 러너가 잡을 잊었으면 RunnerJobLost."""
        with self._client() as client:
            while True:
                resp = client.get(f"/jobs/{job_id}")
                if resp.status_code == 404:
                    raise RunnerJobLost(job_id)
                resp.raise_for_status()
                body = resp.json()
                if body["status"] == "done":
                    return body["result"]
                if body["status"] == "error":
                    raise RunnerJobFailed(body.get("error") or "러너 잡 실패")
                time.sleep(self.poll_interval_sec)

    def transcribe(
        self,
        audio_path: Path,
        *,
        model: str | None,
        language: str | None,
        diarize: bool,
        max_resubmits: int = 2,
    ) -> dict[str, Any]:
        """제출부터 완료까지. 러너 재시작으로 잡이 사라지면 재제출한다."""
        for attempt in range(max_resubmits + 1):
            job_id = self.submit(audio_path, model=model, language=language, diarize=diarize)
            try:
                return self.wait(job_id)
            except RunnerJobLost:
                if attempt == max_resubmits:
                    raise
        raise AssertionError("unreachable")
