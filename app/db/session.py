from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def build_engine(url: str) -> Engine:
    # pool_pre_ping drops stale connections instead of failing the request.
    # connect_timeout stops an unreachable database from hanging requests.
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})


@lru_cache
def get_engine() -> Engine:
    return build_engine(get_settings().database_url)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return make_session_factory(get_engine())


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed afterwards.

    Services commit explicitly; anything uncommitted is rolled back on close.
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
