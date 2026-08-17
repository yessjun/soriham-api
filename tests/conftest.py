from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from soriham_api.config import load_settings
from soriham_api.models import Base


@pytest.fixture(scope="session")
def engine():
    """DATABASE_URL 기준으로 `<db>_test` 데이터베이스를 만들어 쓰고 끝나면 지운다."""
    try:
        url = make_url(load_settings().database_url)
    except RuntimeError:
        pytest.skip("DATABASE_URL 없음 - DB 테스트 생략")
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    test_db = f"{url.database}_test"
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{test_db}"'))
        conn.execute(text(f'CREATE DATABASE "{test_db}"'))
    test_engine = create_engine(url.set(database=test_db))
    Base.metadata.create_all(test_engine)
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
