from functools import lru_cache

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

PSYCOPG_PREFIX = "postgresql+psycopg://"


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def normalize_database_url(url: str) -> str:
    """Accept common Postgres URL spellings and force the psycopg 3 driver."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return PSYCOPG_PREFIX + url[len(prefix):]
    return url


def redact_url(url: str) -> str:
    """Return the URL with the password hidden, safe for logs and errors."""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<unparseable database url>"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    # Optional: tests refuse to run without an explicit test database.
    test_database_url: str | None = None

    @field_validator("database_url", "test_database_url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = normalize_database_url(value.strip())
        if not value.startswith(PSYCOPG_PREFIX):
            # Do not echo the value: it may contain a password.
            raise ValueError("must be a PostgreSQL URL (postgresql+psycopg://...)")
        make_url(value)  # raises if the URL cannot be parsed
        return value


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        # Report which variables are wrong, never their values.
        problems = ", ".join(
            f"{str(err['loc'][0]).upper()} ({err['msg']})" for err in exc.errors()
        )
        raise ConfigurationError(
            f"Invalid configuration: {problems}. "
            "Copy .env.example to .env and set the database variables."
        ) from None
