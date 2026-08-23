from __future__ import annotations

import os
from pathlib import Path

import pytest

from soriham_api.config import load_settings

BASE = {"DATABASE_URL": "postgresql+psycopg://u:p@localhost/db"}


def env(**extra: str) -> dict[str, str]:
    """`load_settings`는 명시 dict에도 레포 .env의 미설정 키를 채운다.

    그래서 단언하는 키는 전부 여기서 명시해야 한다 — 안 그러면 개발 머신의 실값이
    스며들어 테스트가 자기가 넣은 값과 무관하게 통과한다.
    """
    return {**BASE, **extra}


def test_감시_폴더와_업로드_폴더가_겹치면_기동을_거부한다(tmp_path: Path):
    """겹치면 한 사람의 업로드가 스캔에 집혀 다른 워크스페이스의 녹음으로 등록된다.

    오디오 경로 봉쇄 검사로는 못 잡는다 — 파일이 실제로 감시 폴더 아래 있어서 통과한다.
    """
    root = tmp_path / "audio"
    with pytest.raises(RuntimeError, match="겹칩니다"):
        load_settings(env(AUDIO_DIRS=str(root), UPLOAD_DIR=str(root / "uploads")))


def test_업로드_폴더가_감시_폴더를_품어도_거부한다(tmp_path: Path):
    """포함 방향이 반대여도 같은 사고다."""
    up = tmp_path / "store"
    with pytest.raises(RuntimeError, match="겹칩니다"):
        load_settings(env(AUDIO_DIRS=str(up / "audio"), UPLOAD_DIR=str(up)))


def test_같은_폴더를_둘_다_가리키면_거부한다(tmp_path: Path):
    same = tmp_path / "both"
    with pytest.raises(RuntimeError, match="겹칩니다"):
        load_settings(env(AUDIO_DIRS=str(same), UPLOAD_DIR=str(same)))


def test_겹치지_않으면_통과한다(tmp_path: Path):
    settings = load_settings(
        env(
            AUDIO_DIRS=str(tmp_path / "audio"),
            UPLOAD_DIR=str(tmp_path / "uploads"),
            # 개발 머신 .env에 있을 리 없는 값 — 명시값이 무시돼도 통과하지 않게
            DEFAULT_WORKSPACE="ws-for-this-test-only",
        )
    )
    assert settings.default_workspace == "ws-for-this-test-only"


def test_여러_감시_폴더_중_하나만_겹쳐도_거부한다(tmp_path: Path):
    dirs = os.pathsep.join([str(tmp_path / "safe"), str(tmp_path / "uploads" / "nested")])
    with pytest.raises(RuntimeError, match="겹칩니다"):
        load_settings(env(AUDIO_DIRS=dirs, UPLOAD_DIR=str(tmp_path / "uploads")))
