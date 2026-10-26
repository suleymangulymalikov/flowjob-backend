import pytest

from app.core.config import (
    ConfigurationError,
    Settings,
    get_settings,
    normalize_database_url,
    redact_url,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)  # an empty dir, so no developer .env is picked up
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_reads_database_url_from_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    assert get_settings().database_url == "postgresql+psycopg://u:p@localhost:5432/db"


def test_plain_postgres_urls_are_normalized_to_psycopg3():
    assert normalize_database_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_database_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"


def test_missing_database_url_gives_readable_error():
    with pytest.raises(ConfigurationError) as exc:
        get_settings()
    assert "DATABASE_URL" in str(exc.value)
    assert ".env.example" in str(exc.value)


def test_invalid_url_error_does_not_leak_password(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql://user:s3cretpass@localhost/db")
    with pytest.raises(ConfigurationError) as exc:
        get_settings()
    assert "s3cretpass" not in str(exc.value)
    assert "DATABASE_URL" in str(exc.value)


def test_redact_url_hides_password():
    redacted = redact_url("postgresql+psycopg://user:s3cretpass@localhost:5432/db")
    assert "s3cretpass" not in redacted
    assert "user" in redacted and "localhost" in redacted


def test_test_database_url_is_optional(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    assert Settings(_env_file=None).test_database_url is None
