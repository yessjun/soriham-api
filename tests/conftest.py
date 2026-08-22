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
