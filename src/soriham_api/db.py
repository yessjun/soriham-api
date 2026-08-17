"""SQLAlchemy 엔진·세션 팩토리."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from soriham_api.config import Settings


def make_engine(settings: Settings):
    return create_engine(settings.database_url, pool_pre_ping=True)


def make_session_factory(settings: Settings) -> sessionmaker[Session]:
    return sessionmaker(bind=make_engine(settings), expire_on_commit=False)
