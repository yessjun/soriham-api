"""stt 러너 HTTP 잡 API 어댑터. 러너 URL만 알면 로컬·원격 어디든 같은 계약."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


def _is_runner_fault(exc: httpx.HTTPStatusError) -> bool:
    """러너 쪽 문제라 기다리면 풀리는가, 이 요청이 문제라 기다려도 그대로인가.

    5xx는 러너가 넘어진 것이고 429는 잠시 뒤 다시 오라는 뜻이다. 나머지 4xx는 이
    요청에 대한 답이라 재시도해도 같은 답이 온다.
    """
    code = exc.response.status_code
    return code >= 500 or code == 429


def _detail(exc: httpx.HTTPStatusError) -> str:
    """러너가 준 사유를 그대로 옮긴다. 화면에 그 문장이 나간다."""
    try:
        detail = exc.response.json().get("detail")
    except ValueError:
        detail = None
    return f"러너가 거절함 ({exc.response.status_code}): {detail or exc.response.reason_phrase}"


class RunnerUnavailable(Exception):
    """러너에 닿지 못했다. 이 파일의 문제가 아니라 러너 쪽 문제다."""


class RunnerJobLost(Exception):
    """러너가 잡을 잊었다(재시작 등) — 재제출 대상."""


class RunnerJobFailed(Exception):
    """러너가 잡을 error로 끝냈다."""


# (단계, 진행률 0~1 또는 None) — 러너가 진행 정보를 안 주면 (None, None)
ProgressHook = Callable[[str | None, float | None], None]


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
        try:
            return self._submit(data, audio_path)
        except httpx.TransportError as exc:
            # 못 닿은 것과 이 파일이 문제인 것은 다르다. 섞으면 러너가 죽어 있는 동안
            # 대기열 전체가 error로 바뀐다
            raise RunnerUnavailable(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            if _is_runner_fault(exc):
                raise RunnerUnavailable(str(exc)) from exc
            # 4xx는 이 요청이 문제라는 뜻이다(크기 초과, 허용 밖 경로, 파일 없음).
            # 이걸 러너 장애로 다루면 큐로 되돌아가 같은 녹음을 영원히 다시 집는다
            raise RunnerJobFailed(_detail(exc)) from exc

    def _submit(self, data: dict[str, Any], audio_path: Path) -> str:
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

    def wait(self, job_id: str, on_progress: ProgressHook | None = None) -> dict[str, Any]:
        """잡이 끝날 때까지 폴링한다. 러너가 잡을 잊었으면 RunnerJobLost."""
        errors = 0
        with self._client() as client:
            while True:
                try:
                    resp = client.get(f"/jobs/{job_id}")
                    if resp.status_code == 404:
                        raise RunnerJobLost(job_id)
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    if not _is_runner_fault(exc):
                        raise RunnerJobFailed(_detail(exc)) from exc
                    # 장시간 변환 중 폴링 1회 실패로 잡을 버리지 않는다
                    errors += 1
                    if errors > 5:
                        raise RunnerUnavailable(f"러너 응답 없음: {job_id}") from None
                    time.sleep(self.poll_interval_sec)
                    continue
                except httpx.TransportError:
                    errors += 1
                    if errors > 5:
                        raise RunnerUnavailable(f"러너 응답 없음: {job_id}") from None
                    time.sleep(self.poll_interval_sec)
                    continue
                errors = 0
                body = resp.json()
                if body["status"] == "done":
                    return body["result"]
                if body["status"] == "error":
                    raise RunnerJobFailed(body.get("error") or "러너 잡 실패")
                if on_progress is not None:
                    # 진행 필드는 선택이라 없는 러너면 둘 다 None으로 전달된다
                    on_progress(body.get("stage"), body.get("progress"))
                time.sleep(self.poll_interval_sec)

    def transcribe(
        self,
        audio_path: Path,
        *,
        model: str | None,
        language: str | None,
        diarize: bool,
        max_resubmits: int = 2,
        on_progress: ProgressHook | None = None,
    ) -> dict[str, Any]:
        """제출부터 완료까지. 러너 재시작으로 잡이 사라지면 재제출한다."""
        for attempt in range(max_resubmits + 1):
            job_id = self.submit(audio_path, model=model, language=language, diarize=diarize)
            try:
                return self.wait(job_id, on_progress)
            except RunnerJobLost:
                if attempt == max_resubmits:
                    raise
        raise AssertionError("unreachable")
