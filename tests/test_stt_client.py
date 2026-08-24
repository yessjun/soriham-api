from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from soriham_api.stt_client import (
    RunnerClient,
    RunnerJobFailed,
    RunnerJobLost,
    RunnerUnavailable,
)

RESULT = {
    "language": "ko",
    "segments": [
        {
            "start": 0.0,
            "end": 1.0,
            "text": "안녕하세요",
            "speaker": "SPEAKER_00",
            "words": [["안녕하세요", 0.0, 1.0]],
        }
    ],
    "meta": {"device": "cpu", "model": "tiny", "elapsed_sec": 1.0},
}


class FakeRunner:
    """상태 시퀀스를 재생하는 러너 목업."""

    def __init__(self, statuses: list[dict | int]) -> None:
        self.statuses = statuses
        self.submits = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jobs" and request.method == "POST":
            self.submits += 1
            return httpx.Response(200, json={"job_id": f"job-{self.submits}"})
        step = self.statuses.pop(0)
        if isinstance(step, int):
            return httpx.Response(step, json={"detail": "잡이 없습니다"})
        return httpx.Response(200, json=step)


def make_client(fake: FakeRunner) -> RunnerClient:
    return RunnerClient(
        base_url="http://runner.test",
        poll_interval_sec=0.0,
        transport=httpx.MockTransport(fake.handler),
    )


def test_transcribe_polls_until_done(tmp_path: Path):
    fake = FakeRunner(
        [
            {"status": "queued", "result": None, "error": None},
            {"status": "running", "result": None, "error": None},
            {"status": "done", "result": RESULT, "error": None},
        ]
    )
    result = make_client(fake).transcribe(
        tmp_path / "a.wav", model="tiny", language="ko", diarize=True
    )
    assert result == RESULT
    assert fake.submits == 1


def test_transcribe_resubmits_when_runner_forgets(tmp_path: Path):
    fake = FakeRunner([404, {"status": "done", "result": RESULT, "error": None}])
    result = make_client(fake).transcribe(
        tmp_path / "a.wav", model=None, language=None, diarize=True
    )
    assert result == RESULT
    assert fake.submits == 2


def test_transcribe_gives_up_after_max_resubmits(tmp_path: Path):
    fake = FakeRunner([404, 404, 404])
    with pytest.raises(RunnerJobLost):
        make_client(fake).transcribe(
            tmp_path / "a.wav", model=None, language=None, diarize=True, max_resubmits=2
        )
    assert fake.submits == 3


def test_transcribe_raises_on_runner_error(tmp_path: Path):
    fake = FakeRunner([{"status": "error", "result": None, "error": "백엔드 실패"}])
    with pytest.raises(RunnerJobFailed, match="백엔드 실패"):
        make_client(fake).transcribe(tmp_path / "a.wav", model=None, language=None, diarize=True)


def test_upload_mode_sends_file(tmp_path: Path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"pcm")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"job_id": "j"})
        return httpx.Response(200, json={"status": "done", "result": RESULT, "error": None})

    client = RunnerClient(
        base_url="http://runner.test",
        upload=True,
        poll_interval_sec=0.0,
        transport=httpx.MockTransport(handler),
    )
    client.transcribe(audio, model=None, language=None, diarize=True)
    assert b"pcm" in seen[0].read()


def test_러너가_이_파일을_거절하면_큐로_되돌리지_않는다(tmp_path: Path):
    """413이나 403은 이 요청에 대한 답이라 다시 보내도 같은 답이 온다. 러너 장애로
    다루면 큐로 돌아가 같은 녹음을 영원히 다시 집는다."""
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, json={"detail": "업로드 크기 한도를 초과했습니다"})

    client = RunnerClient(
        "http://runner", transport=httpx.MockTransport(handler), poll_interval_sec=0
    )

    with pytest.raises(RunnerJobFailed) as caught:
        client.submit(audio, model=None, language=None, diarize=False)

    assert "413" in str(caught.value)
    assert "크기 한도" in str(caught.value)


def test_러너가_넘어진_것은_큐로_되돌린다(tmp_path: Path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    client = RunnerClient(
        "http://runner", transport=httpx.MockTransport(handler), poll_interval_sec=0
    )

    with pytest.raises(RunnerUnavailable):
        client.submit(audio, model=None, language=None, diarize=False)
