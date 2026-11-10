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

After switching branches or updating API code, restart any backend started without reload.
For the source-verified tailoring frontend, check that
`curl http://127.0.0.1:8000/api/v1/tailor/capabilities` returns
`{"sourceVerification":1}`. This probe does not generate a draft or need a user session.

## Tests

Install MongoDB 8.0 locally on macOS or Linux so the `mongod` executable is available. From the repository root:

```sh
uv sync --locked
env -u MONGODB_URI -u MONGODB_DB_NAME FLOWJOB_TEST_MONGOD=mongod uv run --locked pytest
```

Set `FLOWJOB_TEST_MONGOD=/path/to/mongod` if it isn't on your PATH. You can append a test path or pytest filter as usual. The setting is required even for collection, because test modules import application code.

Pytest starts and verifies its own MongoDB process before collecting tests. It listens only on a private Unix socket and uses a fresh data directory and a randomly named database for each run. The HTTP test fixture clears only that database between tests. Pytest shuts down the process and removes its directory at the end of the run.

The test runner rejects exported MongoDB settings, including Atlas, localhost and an explicit database name. It ignores the development `.env` and sets a throwaway JWT signing secret and `OPENAI_API_KEY=mock`. CI uses the same launcher with MongoDB 8.0 inside the test job.

## AI generation limits

Job Match and AI Tailoring each have a per-user limit for the UTC day
(`MATCH_DAILY_QUOTA=20` and `TAILOR_DAILY_QUOTA=10` by default). New generations in
either workflow count toward the same global limit (`AI_GLOBAL_DAILY_QUOTA=500`).
Cache hits and draft/report reads use no new quota. Per-user exhaustion returns
429 and global exhaustion returns 503.

The backend reserves both quota units in one MongoDB update before calling the
provider. The reservation holds its original UTC day. Successful results keep
both charges after persistence, and failed generations refund both units in
one update. Refunds can safely be repeated. The first reservation of a UTC day
carries forward any usage that the legacy code recorded for that day.

Identical requests in flight in the same backend process share a result or
failure. An explicit fresh tailoring request bypasses cached drafts. Each
generation makes a single OpenAI SDK attempt, with no automatic retries.
A later explicit retry can send a new request after a timeout or other failure.

Workers share the quota through MongoDB and coalesce requests within each
process. Deploy the same quota implementation to all workers. A process crash
or a database outage that prevents settlement can leave a charge for that UTC
day, including when the provider outcome is unknown.

## CV payload and PDF limits

CV create, update, and unsaved-preview requests accept typed browser CV
sections. Sections and their fields can be omitted. Dates, locations, phone
numbers, and languages remain free-form strings. Newly supplied contact links
with active schemes or embedded credentials are rejected. A saved CV's own
historical link text can still be carried forward unchanged and is rendered
inert whenever it is unsafe. Legacy records are projected into the browser CV
shape on read without updating storage or changing their source revision.

The request limit is 256 KiB before parsing. Normalized content is limited to
64 KiB of UTF-8 JSON and 600 experience, education, project, and bullet items
combined. A CV can have up to 100 items per main section, 40 bullets per job,
200 skills and 24 links. For example, summaries can contain 8,000 characters
and bullets can contain 2,000. The API returns 413 for a body over the wire
limit and 422 for invalid fields or over-budget content.

The Harvard template can still render multi-page CVs. Rendering tries at most
five font and margin combinations, skips the intermediate attempts when the
smallest layout still spans pages, and caps output at 16 pages. WeasyPrint
cannot fetch external resources. Saved CVs, unsaved previews and legacy PDF
downloads share two active render slots per backend worker. Additional PDF
requests receive 503 and a short Retry-After header.
