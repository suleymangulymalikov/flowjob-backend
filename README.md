# flowjob-backend

FastAPI backend for FlowJob, using MongoDB (via [Beanie](https://beanie-odm.dev/)).

## Setup

Requires [uv](https://docs.astral.sh/uv/) and a MongoDB database (e.g. a free [MongoDB Atlas](https://cloud.mongodb.com) cluster).

1. Install dependencies. PDF generation ([WeasyPrint](https://weasyprint.org/)) also needs Pango installed on the system:

   ```bash
   brew install pango       # macOS; on Debian/Ubuntu: apt install libpango-1.0-0 libpangoft2-1.0-0
   uv sync
   ```

2. Create a `.env` file from the template and fill in your MongoDB connection string:

   ```bash
   cp .env.example .env
   ```

   | Variable          | Description                                                                  |
   | ----------------- | ---------------------------------------------------------------------------- |
   | `MONGODB_URI`     | Connection string (Atlas: Connect → Drivers → Python), including user and password |
   | `MONGODB_DB_NAME` | Database name, defaults to `flowjob` (created automatically on first write)  |
   | `JWT_SECRET`      | Secret for signing login tokens, generate with `openssl rand -hex 32`        |
   | `ACCESS_TOKEN_EXPIRE_MINUTES` | How long an access token is valid, defaults to `15`              |
   | `REFRESH_TOKEN_EXPIRE_DAYS`   | How long a refresh token is valid, defaults to `30`              |

   In Atlas, also add your IP under **Security → Network Access**.

## Commands

```bash
uv run fastapi dev       # run dev server at http://127.0.0.1:8000 (docs at /docs)
```

The server checks the database connection on startup and exits if it can't reach MongoDB.

## Tests

Install MongoDB 8.0 locally on macOS or Linux so the `mongod` executable is available. From the repository root:

```sh
uv sync --locked
env -u MONGODB_URI -u MONGODB_DB_NAME FLOWJOB_TEST_MONGOD=mongod uv run --locked pytest
```

Set `FLOWJOB_TEST_MONGOD=/path/to/mongod` if it isn't on your PATH. You can append a test path or pytest filter as usual. The setting is required even for collection, because test modules import application code.

Pytest starts and verifies its own MongoDB process before collecting tests. It listens only on a private Unix socket and uses a fresh data directory and a randomly named database for each run. The HTTP test fixture clears only that database between tests. Pytest shuts down the process and removes its directory at the end of the run.

The test runner rejects exported MongoDB settings, including Atlas, localhost and an explicit database name. It ignores the development `.env` and sets a throwaway JWT signing secret and `OPENAI_API_KEY=mock`. CI uses the same launcher with MongoDB 8.0 inside the test job.
