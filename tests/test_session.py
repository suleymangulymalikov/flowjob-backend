import pytest
from sqlalchemy import make_url, text

from app.db import session as session_module
from app.db.base import Base
from app.db.session import build_engine, get_db, make_session_factory


def test_build_engine_uses_pre_ping_and_connect_timeout(monkeypatch):
    captured = {}
    monkeypatch.setattr(session_module, "create_engine", lambda url, **kw: captured.update(kw))
    build_engine("postgresql+psycopg://u:p@localhost:5432/db")
    assert captured["pool_pre_ping"] is True
    assert captured["connect_args"] == {"connect_timeout": 5}


class FakeSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_get_db_closes_session_after_request(monkeypatch):
    monkeypatch.setattr(session_module, "get_session_factory", lambda: FakeSession)
    dependency = get_db()
    session = next(dependency)
    assert not session.closed
    dependency.close()  # what FastAPI does when the request finishes
    assert session.closed


def test_get_db_closes_session_when_request_fails(monkeypatch):
    monkeypatch.setattr(session_module, "get_session_factory", lambda: FakeSession)
    dependency = get_db()
    session = next(dependency)
    with pytest.raises(RuntimeError):
        dependency.throw(RuntimeError("request failed"))
    assert session.closed


def test_base_has_constraint_naming_convention():
    assert {"ix", "uq", "ck", "fk", "pk"} <= set(Base.metadata.naming_convention)


def test_db_session_runs_a_query(db_session):
    assert db_session.execute(text("SELECT 1")).scalar_one() == 1


def test_session_factory_connects_to_the_test_database(test_engine, test_database_url):
    with make_session_factory(test_engine)() as session:
        current = session.execute(text("SELECT current_database()")).scalar_one()
    assert current == make_url(test_database_url).database
