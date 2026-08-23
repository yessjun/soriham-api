"""콘솔 빌드를 같은 오리진에서 서빙한다.

세션 쿠키가 SameSite=Lax라 오디오 태그가 도는 조건이 같은 사이트다. 오리진까지
같으면 CORS 설정 자체가 필요 없다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from conftest import make_settings
from soriham_api.app import create_app


@pytest.fixture
def console(tmp_path: Path) -> Path:
    root = tmp_path / "console"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>소리함</title>")
    (root / "assets" / "app.js").write_text("console.log('ok')")
    return root


@pytest.fixture
def client(engine, console: Path):
    app = create_app(
        settings=make_settings(console_dir=console),
        session_factory=sessionmaker(bind=engine),
    )
    return TestClient(app)


def test_루트에서_콘솔을_준다(client):
    resp = client.get("/")

    assert resp.status_code == 200
    assert "소리함" in resp.text


def test_클라이언트_라우팅_주소도_콘솔을_준다(client):
    """공유 링크 주소로 새로고침하면 서버에는 그런 경로가 없다."""
    assert "소리함" in client.get("/s/어떤토큰").text
    assert "소리함" in client.get("/recordings/abc").text


def test_정적_파일은_그대로_준다(client):
    resp = client.get("/assets/app.js")

    assert resp.status_code == 200
    assert "console.log" in resp.text


def test_API_경로를_삼키지_않는다(client):
    """정적 폴백을 라우트보다 앞에 두면 API가 통째로 index.html이 된다."""
    resp = client.get("/api/auth/me")

    assert resp.status_code == 401
    assert "소리함" not in resp.text


def test_폴더_밖은_내보내지_않는다(client, console: Path, tmp_path: Path):
    """경로에 상위 이동이 섞여도 빌드 폴더 밖 파일이 나가면 안 된다."""
    secret = tmp_path / "secret.txt"
    secret.write_text("시크릿")

    resp = client.get("/../secret.txt")

    assert "시크릿" not in resp.text


def test_설정이_없으면_서빙하지_않는다(engine):
    """개발은 vite 프록시가 같은 오리진을 만든다. 그때는 붙일 이유가 없다."""
    client = TestClient(
        create_app(settings=make_settings(), session_factory=sessionmaker(bind=engine))
    )

    assert client.get("/").status_code == 404
