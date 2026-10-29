from dataclasses import dataclass

from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.core.config import redact_url


@dataclass(frozen=True)
class DatabaseHealth:
    ok: bool
    detail: str


def _hint(message: str) -> str:
    lowered = message.lower()
    if "password authentication failed" in lowered or "authentication failed" in lowered:
        return "Authentication failed: check the user and password in DATABASE_URL."
    if "does not exist" in lowered and "database" in lowered:
        return "The database does not exist: create it or fix the name in DATABASE_URL."
    if "connection refused" in lowered or "could not connect" in lowered:
        return "Connection refused: is PostgreSQL running (docker compose up -d db) and is the port right?"
    if "timeout" in lowered or "timed out" in lowered:
        return "Connection timed out: check the host and port."
    if "could not translate host name" in lowered or "name or service not known" in lowered:
        return "Host not found: check the host in DATABASE_URL."
    return "Could not connect to the database."


def check_database(engine: Engine) -> DatabaseHealth:
    """Verify PostgreSQL connectivity.

    Never returns raw driver messages or the connection string, because they
    can contain credentials. Only the redacted URL and a short hint are returned.
    """
    target = redact_url(engine.url.render_as_string(hide_password=False))
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        return DatabaseHealth(False, f"{_hint(str(exc.orig))} ({target})")
    except SQLAlchemyError:
        return DatabaseHealth(False, f"Database check failed ({target}).")
    return DatabaseHealth(True, f"Connected ({target})")
