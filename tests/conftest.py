from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from soriham_api.config import load_settings
from soriham_api.models import Base
from soriham_api.tenancy import add_member, create_user, create_workspace

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


@pytest.fixture(scope="session")
def engine():
    """DATABASE_URL 기준으로 `<db>_test`를 만들어 마이그레이션으로 채우고 끝나면 지운다.

    스키마를 `create_all`이 아니라 alembic으로 세우는 이유: 모델과 마이그레이션이
    갈라져도 `create_all` 경로에서는 영원히 드러나지 않는다. 테스트가 실제로 배포되는
    스키마를 쓰게 해야 그 차이가 테스트에서 터진다.
    """
    try:
        url = make_url(load_settings().database_url)
    except RuntimeError:
        pytest.skip("DATABASE_URL 없음 - DB 테스트 생략")
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    test_db = f"{url.database}_test"
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{test_db}"'))
        conn.execute(text(f'CREATE DATABASE "{test_db}"'))
    test_url = url.set(database=test_db)

    cfg = Config(str(ALEMBIC_INI))
    # set_main_option 값은 configparser 보간을 거친다 — 비밀번호에 %가 들어가면
    # 보간 문법 오류로 죽으므로 미리 이스케이프한다. env.py가 읽을 때 되돌아온다
    url_text = test_url.render_as_string(hide_password=False).replace("%", "%%")
    cfg.set_main_option("sqlalchemy.url", url_text)
    command.upgrade(cfg, "head")

    test_engine = create_engine(test_url)
    yield test_engine
    test_engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{test_db}"'))
    admin.dispose()


@pytest.fixture
def db(engine):
    with Session(engine, expire_on_commit=False) as session:
        yield session
        session.rollback()
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture
def workspace(db):
    """녹음이 소속될 워크스페이스. 테넌시가 생긴 뒤로는 거의 모든 테스트가 하나 필요하다."""
    ws = create_workspace(db, slug="test-ws", name="테스트")
    db.commit()
    return ws


@pytest.fixture
def other_workspace(db):
    """격리를 확인할 때 쓰는 두 번째 워크스페이스."""
    ws = create_workspace(db, slug="other-ws", name="다른 곳")
    db.commit()
    return ws


@pytest.fixture
def owner(db, workspace):
    """workspace의 소유자. 대부분의 API 테스트가 이 사람으로 로그인한다."""
    from soriham_api import auth

    user = create_user(
        db,
        email="owner@example.com",
        password_hash=auth.hash_password(TEST_PASSWORD),
        display_name="소유자",
        status="active",
    )
    add_member(db, workspace, user, "owner")
    user.default_workspace_id = workspace.id
    db.commit()
    return user


TEST_PASSWORD = "시험용 암구호"


def login(client, email: str, password: str = TEST_PASSWORD):
    """로그인하고 CSRF 헤더를 클라이언트에 심는다.

    쿠키는 TestClient가 알아서 들고 다닌다. 헤더는 브라우저가 자동으로 붙여주지
    않으므로(그게 CSRF 방어의 요지다) 여기서 직접 넣는다.
    """
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    client.headers["x-csrf-token"] = client.cookies["soriham_csrf"]
    return resp.json()


def make_settings(**overrides):
    """테스트용 설정. 새 필드가 늘 때 테스트 파일마다 고치지 않게 한 곳에 둔다."""
    from soriham_api.config import Settings

    base = dict(
        database_url="unused",
        audio_dirs=(),
        runner_url="http://runner.test",
        runner_upload=False,
        stt_model=None,
        stt_language=None,
        cors_origins=("http://localhost:5174",),
        upload_dir=None,
        max_upload_bytes=4 * 1024 * 1024 * 1024,
        default_workspace=None,
        cookie_name="soriham_session",
        # TestClient는 http로 부르므로 Secure 쿠키를 붙이면 되돌아오지 않는다
        cookie_secure=False,
        cookie_domain=None,
        auto_approve=False,
        console_dir=None,
        # 운영 기본값과 같은 모양으로 둔다. 여기만 무제한으로 두면 "승인이 곧 무제한"
        # 이라는 결함이 테스트에서 영원히 안 보인다
        default_quota_minutes=600,
        default_quota_bytes=20 * 1024 * 1024 * 1024,
        expose_docs=False,
        enrich_backend="off",
        ollama_url="http://localhost:11434",
        ollama_model="qwen3:8b",
    )
    base.update(overrides)
    return Settings(**base)
