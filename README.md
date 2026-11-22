# FlowJob Backend

FlowJob helps students and early-career job seekers build a strong CV, check how well it fits a job post, tailor
it for that job, and keep track of every application. It is a team project at Wrocław University of Science and
Technology.

This repository contains the **REST API**, built with FastAPI and MongoDB. The web app is in
[flowjob-frontend](https://github.com/Youssif-El-menshawy/flowjob-frontend).

## Features

- **Accounts and sessions**: registration and login with Argon2-hashed passwords, short-lived JWT access tokens,
  single-use refresh tokens in an HttpOnly cookie, and rate limits on login and registration.
- **Profile and CVs**: a reusable profile and any number of saved CVs, rendered as a Harvard-style PDF with
  WeasyPrint for preview and download.
- **Job Matching**: OpenAI extracts the job's requirements and quotes evidence from the CV; the backend verifies
  every quotation and calculates the score itself. Results are cached and limited per user and per day.
- **Smart CV Import**: turns the selectable text of an uploaded PDF CV into the CV Builder's structure for the
  user to review. Every value is checked against the PDF's own text; nothing is stored until the user saves the
  CV.
- **CV Tailoring**: proposes typed, verified changes (reorders, rewrites, new skills) for a specific job. Only
  changes the user approves are applied, and the result is saved as a new CV; the original is never modified.
- **Applications**: track applications with status, history, follow-up dates, notes and the CV that was sent.
- **Gmail integration (optional)**: a read-only Gmail connection that finds recruitment emails, imports clearly
  confirmed applications and suggests status changes, which the user approves in Needs Review.

## Tech stack

| Area | Technology |
| --- | --- |
| Language and runtime | Python 3.12, [uv](https://docs.astral.sh/uv/) |
| Web framework | [FastAPI](https://fastapi.tiangolo.com/) with Uvicorn |
| Database | MongoDB with the [Beanie](https://beanie-odm.dev/) ODM (PyMongo async driver) |
| Authentication | PyJWT, pwdlib (Argon2) |
| AI | [OpenAI](https://platform.openai.com/) Responses API with structured outputs (default model `gpt-4.1`) |
| Email | Gmail API and Google OAuth 2.0 (read-only scope), AES-256-GCM token encryption (`cryptography`) |
| PDF | [WeasyPrint](https://weasyprint.org/) with a Jinja2 template; [pypdf](https://pypdf.readthedocs.io/) for reading uploaded CVs |
| Tests and CI | pytest with a private MongoDB per run, GitHub Actions |

## Project structure

```
app/
  main.py            FastAPI app, routers, start-up and shutdown
  config.py          settings, read from environment variables and .env
  db.py              MongoDB connection and Beanie models
  auth/              register, login, token refresh, rate limits
  users/, profiles/  account and profile
  integration/       browser API: profile, saved CVs, CV PDFs, account settings
  cv/                CV validation and Harvard PDF rendering (templates/harvard.html)
  cv_import/         Smart CV Import: isolated PDF text extraction, checks against the PDF text, endpoint
  matches/           Job Matching API
  ai_matching/       requirement extraction, citation checks and scoring
  tailor/            CV Tailoring API and verification of suggested changes
  applications/      applications and their history
  gmail/             Gmail OAuth, sync, classification, Needs Review and scheduler
  services/          OpenAI calls, caching and daily quotas
tests/               pytest suite
scripts/tailor_eval/ optional tailoring evaluation script (makes paid OpenAI calls)
docs/                detailed operations and Gmail documentation
```

## Prerequisites

- **Python 3.12** and **uv**
- **MongoDB**: a local MongoDB 8.0 server or a MongoDB Atlas cluster
- **Pango**, required by WeasyPrint for PDF rendering:
  `brew install pango` on macOS, or `apt install libpango-1.0-0 libpangoft2-1.0-0` on Debian/Ubuntu
- Optional: an **OpenAI API key** for Job Matching, Tailoring and Gmail analysis
- Optional: a **Google Cloud OAuth client** for the Gmail integration

## Getting started

1. Install the dependencies:

   ```bash
   uv sync --locked
   ```

2. Create your local configuration from the template and fill in the required values (see
   [Configuration](#configuration)):

   ```bash
   cp .env.example .env
   ```

3. Start the API:

   ```bash
   uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
   ```

   The API runs at `http://127.0.0.1:8000`. Interactive documentation is at `/docs`, and `GET /health` returns
   `{"status": "ok"}`.

4. Start the [frontend](https://github.com/Youssif-El-menshawy/flowjob-frontend) on `http://localhost:5173`. Its
   development server forwards `/api/v1` requests to this API.

## Configuration

Settings are read from environment variables or from a `.env` file in the repository root. `.env.example` lists
every variable with comments. Never commit `.env`; it is ignored by Git.

| Variable | Required | Purpose |
| --- | --- | --- |
| `MONGODB_URI` | yes | MongoDB connection string (local server or Atlas) |
| `MONGODB_DB_NAME` | no | Database name, default `flowjob` |
| `JWT_SECRET` | yes | Secret for signing access tokens. Generate with `openssl rand -hex 32` |
| `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS` | no | Session lifetimes, default 15 minutes and 30 days |
| `COOKIE_SECURE` | no | `false` for local HTTP, `true` in production (HTTPS) |
| `OPENAI_API_KEY` | no | Enables the AI features. Leave empty to disable them (the AI endpoints return 503) |
| `OPENAI_MODEL` | no | Model for all AI features, default `gpt-4.1` |
| `MATCH_DAILY_QUOTA`, `TAILOR_DAILY_QUOTA`, `IMPORT_DAILY_QUOTA`, `AI_GLOBAL_DAILY_QUOTA` | no | Daily AI limits per user (CV import: 5) and in total |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GMAIL_TOKEN_KEY` | no | Enable the Gmail integration |
| `GOOGLE_OAUTH_REDIRECT_URI`, `FRONTEND_BASE_URL` | no | Gmail OAuth redirect and the frontend address |
| `GMAIL_SCHEDULER_ENABLED` | no | Background Gmail sync every 3 hours, default `false` |

The OpenAI and Gmail features are optional. Without their settings, everything else works and those features
report that they are unavailable.

### Mock AI mode

`OPENAI_API_KEY=mock` returns fixed, invented AI results without calling OpenAI. It exists for automated tests and
free local development, so it is guarded:

- It only runs together with `ALLOW_MOCK_AI=true`. A `mock` key without it stops the backend at start-up.
- It is refused, even when allowed, if `COOKIE_SECURE=true` (an HTTPS deployment).
- While it is on, the backend logs `MOCK AI MODE is ON` at start-up and a warning the first time each AI feature
  returns a canned result.

Never enable mock AI in a deployed environment.

## Database

FlowJob uses a single MongoDB database, named by `MONGODB_DB_NAME`. No manual setup is needed:

- On start-up the API pings MongoDB and exits if it cannot connect.
- Beanie then registers the document models and creates their collections' indexes, including TTL indexes that
  remove expired refresh tokens and rate-limit records. The database user therefore needs permission to create
  indexes.
- Collections are created automatically.

For MongoDB Atlas, also allow your IP address under **Security → Network Access**.

## API overview

The web app uses the routes under `/api/v1`. Some older routes (`/auth`, `/users`, `/profile` and `/cv/pdf`) are
still available. Full request and response schemas are in the interactive documentation at `/docs`.

| Area | Routes |
| --- | --- |
| Authentication | `POST /api/v1/auth/register`, `/login`, `/refresh`, `/logout` |
| Account | `PATCH /api/v1/me`, `POST /api/v1/me/password` |
| Profile | `GET`, `PUT /api/v1/profile` |
| CVs | `/api/v1/cvs` (list, create, read, update, delete), `GET /api/v1/cvs/{id}/pdf`, `POST /api/v1/cvs/preview/pdf` |
| Smart CV Import | `POST /api/v1/cvs/import` (see [docs/operations.md](docs/operations.md#smart-cv-import)) |
| Job Matching | `POST`, `GET /api/v1/matches`, `GET`, `DELETE /api/v1/matches/{id}` |
| CV Tailoring | `POST /api/v1/tailor`, `GET /api/v1/tailor/{id}`, `GET /api/v1/tailor/capabilities` |
| Applications | `/api/v1/applications` (list, create, read, update, delete) |
| Gmail | `GET`, `DELETE /api/v1/gmail`, OAuth start and callback, `POST /api/v1/gmail/sync`, Needs Review under `/api/v1/gmail/reviews` |
| Health | `GET /health` |

CORS allows `http://localhost:5173` and `http://127.0.0.1:5173`. In development the frontend calls the API through
its own proxy, so the browser stays on one origin.

## Testing

Tests need MongoDB 8.0 installed locally, so that the `mongod` executable is available. Run from the repository
root:

```bash
uv sync --locked
```

```bash
env -u MONGODB_URI -u MONGODB_DB_NAME FLOWJOB_TEST_MONGOD=mongod uv run --locked pytest
```

Set `FLOWJOB_TEST_MONGOD=/path/to/mongod` if it is not on your `PATH`. You can append a test file or `-k` filter as
usual.

The test suite is isolated from real data and services:

- pytest starts its own MongoDB process on a private Unix socket, with a new data directory and a randomly named
  database, and removes them afterwards.
- It refuses to run if MongoDB settings (Atlas, localhost or a database name) are exported in the shell, and it
  ignores the development `.env`.
- It uses a throwaway JWT secret and mock AI (`OPENAI_API_KEY=mock` with `ALLOW_MOCK_AI=true`).
- Google OAuth, the Gmail API and the AI model are replaced with deterministic fakes. No test contacts Google or
  OpenAI.

GitHub Actions runs the same suite with MongoDB 8.0 on every pull request and on pushes to `main`
(`.github/workflows/backend-tests.yml`).

## Gmail integration (optional)

Gmail is an optional, read-only connection on the Applications page (scopes `gmail.readonly`, `openid` and
`email`). It is unavailable until `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` and `GMAIL_TOKEN_KEY` are
set. Full email bodies and access tokens are never stored, and the refresh token is encrypted. See
[docs/gmail.md](docs/gmail.md) for how it works, the scheduler, key rotation, and privacy and data retention.

### Local setup

1. In Google Cloud Console, create or choose a project and enable the **Gmail API**.
2. Configure the **OAuth consent screen** with user type **External** and publishing status **Testing**. Add the
   scopes `openid`, `.../auth/userinfo.email` and `https://www.googleapis.com/auth/gmail.readonly`.
3. Add each tester's Google address under **Test users** (Testing mode allows at most 100).
4. Create **Credentials → OAuth client ID → Web application** with the authorized redirect URI
   `http://localhost:5173/api/v1/gmail/oauth/callback`. Use `localhost` in the browser too, so the OAuth cookie
   stays on the same origin.
5. Put the client ID and secret into your `.env`, generate `GMAIL_TOKEN_KEY` with the command in `.env.example`,
   and optionally set `GMAIL_SCHEDULER_ENABLED=true`.
6. Restart the backend; settings are read at start-up.

### Production configuration

Put real values in the deployment's secret store or environment only.

| Setting | Production value |
| --- | --- |
| `GOOGLE_OAUTH_REDIRECT_URI` | `https://<frontend host>/api/v1/gmail/oauth/callback`, registered identically in Google Cloud |
| `FRONTEND_BASE_URL` | `https://<frontend host>` |
| `COOKIE_SECURE` | `true` |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | from the Google Cloud OAuth "Web application" client |
| `GMAIL_TOKEN_KEY` (and `GMAIL_TOKEN_KEYS_OLD` while rotating) | see [Key management and rotation](docs/gmail.md#key-management-and-rotation) |
| `GMAIL_SCHEDULER_ENABLED` | `true` on long-running backend processes |

The OAuth start endpoint and the callback must be served on the **same host as the frontend**. Route `/api/v1/*`
on that host to the backend with a reverse proxy, as the Vite proxy does in development. See
[Deployment requirements](docs/gmail.md#deployment-requirements) for HTTPS and logging requirements.

## Further documentation

- [docs/operations.md](docs/operations.md): AI usage limits, CV and PDF limits, Smart CV Import, and
  authentication settings for deployment
- [docs/gmail.md](docs/gmail.md): Gmail integration details, scheduler, key management, privacy and data retention
- [scripts/tailor_eval/README.md](scripts/tailor_eval/README.md): the tailoring evaluation script. It calls OpenAI
  for real when a real key is configured, which costs money, and creates throwaway accounts in the configured
  database, so only run it against a development database.

## Known limits

- **AI output is checked, not proven true.** The backend verifies tailoring targets, reorder permutations, and that
  an added skill is named in a verbatim quotation from the CV. It cannot prove that a rewritten sentence adds no new
  fact, so rewrites always need the user's approval and a truthfulness confirmation. In matching, quotations are
  verified word for word, but whether a quotation is relevant evidence is left to the prompt.
- **Profile data has two shapes.** `Profile.frontend_data` holds what the browser edits and is the source of truth
  for those fields. Edits through the older profile routes do not update it automatically.
- **Production hardening is still needed.** Before a public deployment, review HTTPS and cookie settings, allowed
  origins, routing, login abuse protection, and cookie/CSRF behaviour for the chosen host.
- **Not implemented yet:** password reset by email, data export and account deletion.

## Related repository

- Frontend: [Youssif-El-menshawy/flowjob-frontend](https://github.com/Youssif-El-menshawy/flowjob-frontend)
