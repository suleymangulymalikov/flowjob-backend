# flowjob-backend

FastAPI backend for FlowJob.

Stack: Python 3.12, FastAPI, PostgreSQL 16, SQLAlchemy 2 (sync) with psycopg 3, Alembic, pytest.
Dependencies are managed with [uv](https://docs.astral.sh/uv/) (`pyproject.toml` + `uv.lock`).

## Setup

Requires [uv](https://docs.astral.sh/uv/) and [Docker](https://www.docker.com/) (for PostgreSQL).

```bash
uv sync                  # install dependencies
uv run fastapi dev       # run dev server at http://127.0.0.1:8000 (docs at /docs)
uv run pytest            # run tests
```

## Quick start with the database

```bash
# 1. Install dependencies
uv sync

# 2. Configure the environment
cp .env.example .env               # Windows (PowerShell): Copy-Item .env.example .env
#    Optionally choose your own password. Use the same password in all three places:
#    POSTGRES_PASSWORD, DATABASE_URL and TEST_DATABASE_URL.

# 3. Start PostgreSQL
docker compose up -d db

# 4. Apply migrations and run the API
uv run alembic upgrade head
uv run fastapi dev

# 5. Check it works (in a second terminal)
curl http://127.0.0.1:8000/health/db   # Windows (PowerShell): curl.exe ...
uv run pytest
```

## PostgreSQL for local development

`docker-compose.yml` runs **PostgreSQL 16** in a container called `flowjob-db`, with a named
volume (`flowjob_pgdata`). On the first start it creates two databases:

| Database       | Used for                                   |
| -------------- | ------------------------------------------ |
| `flowjob_dev`  | Running the app locally                    |
| `flowjob_test` | Automated tests only (separate on purpose) |

Everyday commands:

```bash
docker compose up -d db        # start
docker compose stop db         # stop (data is kept)
docker compose logs -f db      # logs
docker compose down -v         # DELETE the database and start from scratch
```

The test database is created by `docker/postgres/init/01-create-test-db.sh`, which only runs when
the volume is created. If you change the password or database names after the first start, run
`docker compose down -v` and start again.

**Without Docker:** install PostgreSQL 16, then create the role and databases yourself:

```sql
CREATE ROLE flowjob LOGIN PASSWORD 'your_password';
CREATE DATABASE flowjob_dev OWNER flowjob;
CREATE DATABASE flowjob_test OWNER flowjob;
```

Everything is local. No shared or production database is needed or used.

## Environment variables

Configured in `.env` (git-ignored). `.env.example` is the template and contains placeholders only.
**Never commit real credentials.**

| Variable            | Used by          | Description                                                        |
| ------------------- | ---------------- | ------------------------------------------------------------------ |
| `DATABASE_URL`      | app, Alembic     | e.g. `postgresql+psycopg://flowjob:PASSWORD@localhost:5432/flowjob_dev` |
| `TEST_DATABASE_URL` | pytest           | Must be a different database, and its name must contain `test`     |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_TEST_DB`, `POSTGRES_PORT` | Docker Compose | Create the local database container |

`postgresql://` and `postgres://` URLs are accepted and converted to the psycopg 3 driver. If
`DATABASE_URL` is missing or invalid, you get a `ConfigurationError` naming the variable (the
password is never printed): from Alembic straight away, and from the app on the first request that
needs the database (`/health/db` shows it as a 503).

## Database code layout

```
app/core/config.py      Typed settings (DATABASE_URL, TEST_DATABASE_URL)
app/db/base.py          SQLAlchemy Base with a constraint naming convention
app/db/session.py       Engine, session factory and the get_db FastAPI dependency
app/db/health.py        check_database(): connectivity check with redacted errors
migrations/             Alembic environment and versions
tests/conftest.py       Test database fixtures
```

Use the session in a route like this:

```python
from fastapi import Depends
from sqlalchemy.orm import Session
from app.db.session import get_db

@router.get("/example")
def example(db: Session = Depends(get_db)):
    ...
```

`get_db` opens one session per request and always closes it, returning the connection to the pool
even if the request fails. Services commit explicitly; uncommitted work is rolled back.

## Migrations (Alembic)

The database URL comes from `DATABASE_URL`. It is not stored in `alembic.ini`.

```bash
uv run alembic upgrade head                           # apply all migrations
uv run alembic downgrade -1                           # reverse the last migration
uv run alembic downgrade base                         # reverse everything
uv run alembic current                                # show the current revision
uv run alembic history                                # list revisions
uv run alembic revision --autogenerate -m "add users" # create a migration from model changes
uv run alembic revision -m "describe the change"      # create an empty migration
uv run alembic upgrade head --sql                     # print the SQL without running it
uv run alembic check                                  # fail if models and migrations differ
```

Rules of thumb: always read an autogenerated migration before committing it, and make sure it has
a working `downgrade`. New model modules must be imported in `migrations/env.py` so autogenerate
can see them. The current baseline revision (`0001`) is empty: tables are added by later tasks.

## Health check

- `GET /health` returns `200 {"status": "ok"}` when the API is running (no database needed).
- `GET /health/db` returns `200 {"status": "ok", "detail": "Connected (...)"}` when PostgreSQL
  answers, or `503 {"status": "unavailable", "detail": "..."}` with a short explanation.

In code, use `check_database(engine)` from `app/db/health.py`. Messages contain only the redacted
URL (password shown as `***`) and a hint such as "Connection refused" or "database does not exist".

## Tests

```bash
uv run pytest
```

Database tests use `TEST_DATABASE_URL`. They migrate that database, and every test runs inside a
transaction that is rolled back, so tests never leave data behind and never touch `flowjob_dev`.
If `TEST_DATABASE_URL` is not set, database tests are skipped. pytest refuses to run if it points
at the same database as `DATABASE_URL` or if the database name does not contain `test`.

Fixtures: `test_engine` (migrated engine), `db_session` (rolled-back session), `client` (FastAPI
test client using that session).

## Common problems

| Symptom | Likely cause and fix |
| ------- | -------------------- |
| `Invalid configuration: DATABASE_URL (Field required)` | No `.env`. Copy `.env.example` to `.env`. |
| `Connection refused` / `Connection timed out` / `/health/db` returns 503 | PostgreSQL is not running (`docker compose up -d db`), or the port in the URL is wrong. |
| `/health/db` takes about 10 seconds to return 503 (Windows) | Normal when the database is down: Windows waits for the 5 second connect timeout, once for IPv6 and once for IPv4 `localhost`. |
| `Authentication failed` | The password in `DATABASE_URL` differs from `POSTGRES_PASSWORD`. If you changed it after the first start, run `docker compose down -v`. |
| `database "flowjob_test" does not exist` | The volume was created before the init script existed. Run `docker compose down -v` and start again, or create it manually. |
| `container name "/flowjob-db" is already in use` | An old container with that name exists (for example from another checkout). Remove it with `docker rm flowjob-db`. |
| `port is already allocated` / port 5432 in use | Another PostgreSQL is running. Stop it or set `POSTGRES_PORT=5433` and use that port in the URLs. |
| `Cannot connect to the Docker daemon` / `dockerDesktopLinuxEngine` not found | Start Docker Desktop. |
| Init script fails on Windows (`bad interpreter`, `\r`) | The script has CRLF line endings. `.gitattributes` forces LF; delete the file and check it out again. |
| `uv` is not recognized | uv is not installed or not on PATH. Install it and restart the terminal / VS Code. |
| Tests are skipped | `TEST_DATABASE_URL` is not set in `.env`. |
| `Refusing to run tests against database ...` | The test database name must contain `test`. |
