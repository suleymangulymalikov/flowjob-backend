import pytest
from fastapi.testclient import TestClient

from app import main
from app.core.config import ConfigurationError
from app.db.health import _hint, check_database
from app.db.session import build_engine
from app.main import app

client = TestClient(app)

SECRET = "s3cretpass"


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_check_database_succeeds_against_real_postgres(test_database_url):
    engine = build_engine(test_database_url)
    try:
        result = check_database(engine)
    finally:
        engine.dispose()
    assert result.ok
    assert "Connected" in result.detail


def test_unreachable_server_reports_failure_without_password():
    engine = build_engine(f"postgresql+psycopg://flowjob:{SECRET}@localhost:59999/flowjob_test")
    try:
        result = check_database(engine)
    finally:
        engine.dispose()
    assert not result.ok
    assert SECRET not in result.detail
    assert "localhost:59999" in result.detail


def test_missing_database_reports_failure_without_password(test_database_url):
    bad_url = test_database_url.rsplit("/", 1)[0] + "/no_such_db"
    engine = build_engine(bad_url)
    try:
        result = check_database(engine)
    finally:
        engine.dispose()
    assert not result.ok
    assert "does not exist" in result.detail
    assert "no_such_db" in result.detail


@pytest.mark.parametrize(
    "message, expected",
    [
        ('FATAL: password authentication failed for user "x"', "Authentication failed"),
        ('FATAL: database "x" does not exist', "does not exist"),
        ("connection refused", "Connection refused"),
        ("connection timeout expired", "timed out"),
        ("something unexpected", "Could not connect"),
    ],
)
def test_hint_maps_common_errors(message, expected):
    assert expected in _hint(message)


def test_health_db_endpoint_ok(test_database_url, monkeypatch):
    engine = build_engine(test_database_url)
    monkeypatch.setattr(main, "get_engine", lambda: engine)
    try:
        response = client.get("/health/db")
    finally:
        engine.dispose()
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_db_endpoint_503_when_database_down(monkeypatch):
    engine = build_engine(f"postgresql+psycopg://flowjob:{SECRET}@localhost:59999/x")
    monkeypatch.setattr(main, "get_engine", lambda: engine)
    try:
        response = client.get("/health/db")
    finally:
        engine.dispose()
    assert response.status_code == 503
    assert SECRET not in response.text


def test_health_db_endpoint_503_when_not_configured(monkeypatch):
    def boom():
        raise ConfigurationError("Invalid configuration: DATABASE_URL (Field required).")

    monkeypatch.setattr(main, "get_engine", boom)
    response = client.get("/health/db")
    assert response.status_code == 503
    assert "DATABASE_URL" in response.json()["detail"]
