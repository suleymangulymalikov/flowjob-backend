"""Shared test fixtures.

Database tests run against a separate database (TEST_DATABASE_URL, e.g.
flowjob_test), never the developer's DATABASE_URL. Each test runs inside a
transaction that is rolled back, so tests cannot leak data into each other.
"""
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from sqlalchemy import make_url
from sqlalchemy.orm import Session

from app.db.session import build_engine, get_db

ROOT = Path(__file__).resolve().parents[1]


def _lookup(name: str) -> str | None:
    """Environment variable first, then the git-ignored .env file."""
    return os.environ.get(name) or dotenv_values(ROOT / ".env").get(name)


def assert_safe_test_url(test_url: str, dev_url: str | None) -> None:
    """Refuse to run if the tests could touch the development database."""
    test_db = make_url(test_url).database or ""
    if dev_url and make_url(dev_url).render_as_string(hide_password=True) == make_url(
        test_url
    ).render_as_string(hide_password=True):
        raise pytest.UsageError("TEST_DATABASE_URL must not be the same as DATABASE_URL.")
    if "test" not in test_db.lower():
        raise pytest.UsageError(
            f"Refusing to run tests against database '{test_db}': its name must contain 'test'."
        )


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = _lookup("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set (see .env.example); skipping database tests")
    assert_safe_test_url(url, _lookup("DATABASE_URL"))
    return url


def alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = url
    return config


@pytest.fixture(scope="session")
def test_engine(test_database_url):
    """Engine for the test database, migrated to the latest revision."""
    command.upgrade(alembic_config(test_database_url), "head")
    engine = build_engine(test_database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(test_engine):
    """A session whose work is rolled back at the end of the test."""
    connection = test_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client(db_session):
    """FastAPI test client whose get_db dependency uses the rolled-back session."""
    from fastapi.testclient import TestClient

    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
