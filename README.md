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

## Authentication and deployment

Browser and legacy authentication share MongoDB fixed windows of 15 minutes.
Login allows 40 attempts per IPv4 address or IPv6 /64 and 10 per normalized
email address. Registration allows 12 attempts per network. The limits count
validated submissions, including successes, and return 429 with Retry-After.
The MongoDB user needs permission to create the TTL index for the
`auth_rate_limits` collection. The collection stores keyed digests and
attempt counts. All workers must use the same JWT signing secret and database
to enforce the same limits.

Keep the frontend and the browser API on the same site in production. Serve
them over HTTPS and set `COOKIE_SECURE=true` in the backend environment,
including when a proxy terminates TLS. The refresh cookie is host-only,
HttpOnly and SameSite=Lax. HTTPS requests always set Secure. Local HTTP
development works with `COOKIE_SECURE=false`. If using a reverse proxy,
configure Uvicorn's `--forwarded-allow-ips` to trust only that proxy. The proxy
must set the client address from the real connection. FlowJob uses the ASGI
client address and does not parse client-supplied forwarding headers.

Authentication payloads are capped at 16 KiB. New passwords use 8–256 Unicode
characters without control characters. Login and current-password checks
allow existing passwords up to 1,024 Unicode characters without rehashing
stored credentials. Each backend worker runs up to four Argon2 operations at
a time off the event loop.

Refresh tokens remain single-use. Logout revokes the current refresh token.
An access JWT already issued remains valid until its expiry, 15 minutes by
default. The browser shares refresh work within its current tab and waits for
an in-flight refresh before sending logout so the rotated cookie can be
revoked.

## Gmail integration (optional)

Gmail lives on the Applications page. FlowJob login is unchanged; Gmail is an optional, read-only connection
(`gmail.readonly` plus `openid email`). Without `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` and
`GMAIL_TOKEN_KEY`, `GET /api/v1/gmail` reports `available: false` and nothing else changes.

What it does: after connecting, the backend lists the last 30 days of mail (Spam/Trash excluded, Promotions
included), filters candidates by metadata, minimizes each candidate (quoted history, signatures, contact details
and tracking URLs removed; attachments never read) and asks the model for structured, multilingual output. The
backend verifies quotes and references and decides every write: strongly evidenced applications are imported
(status Applied, no CV), uncertain ones go to Needs Review, and status changes of existing applications always
need approval. Full email bodies and access tokens are never stored; the refresh token is AES-256-GCM encrypted.
It syncs every 3 hours while the backend runs (`GMAIL_SCHEDULER_ENABLED=true`) and on Sync now. Failed emails wait
for Retry sync; temporary Gmail outages and rate limits are retried automatically with backoff. Disconnect revokes
the grant and erases Gmail data (credentials, excerpts, suggestions, dismissals, progress); applications and approved
statuses stay. Times stated with an unknown or conflicting time zone, or falling in a daylight-saving gap or overlap,
are never guessed: the activity is recorded without a time and the user enters it in Needs Review.

### Setting up Google OAuth for local testing (done by the project owner, not by code)

1. In Google Cloud Console, create (or choose) a project and enable the **Gmail API**.
2. Configure the **OAuth consent screen**: user type **External**, publishing status **Testing**. Add the scopes
   `openid`, `.../auth/userinfo.email` and `https://www.googleapis.com/auth/gmail.readonly`.
3. Add each tester's Google address under **Test users** (Testing mode allows at most 100).
4. Create **Credentials → OAuth client ID → Web application**. Add the Authorized redirect URI
   `http://localhost:5173/api/v1/gmail/oauth/callback` (the Vite dev proxy keeps the binding cookie same-origin;
   use the same host name, `localhost`, in the browser).
5. Put the client id and secret into your backend `.env` (never commit it), generate `GMAIL_TOKEN_KEY` with the
   command in `.env.example`, set `FRONTEND_BASE_URL`, and optionally `GMAIL_SCHEDULER_ENABLED=true`.
6. Restart the backend (settings are read at start-up).

### Production configuration

No secret appears in this repository; put real values in the deployment's secret store or environment only (never in
a `VITE_*` variable, which is public).

| Setting | Production value |
| --- | --- |
| `GOOGLE_OAUTH_REDIRECT_URI` | `https://<frontend host>/api/v1/gmail/oauth/callback`, registered identically in Google Cloud |
| `FRONTEND_BASE_URL` | `https://<frontend host>` (the callback redirects to `/app/applications`) |
| `COOKIE_SECURE` | `true` |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | from the Google Cloud OAuth "Web application" client |
| `GMAIL_TOKEN_KEY` (+ `GMAIL_TOKEN_KEYS_OLD` while rotating) | see "Key management" below |
| `GMAIL_SCHEDULER_ENABLED` | `true` on the long-running backend processes |

* **Same origin for the OAuth flow.** `POST /api/v1/gmail/oauth/start` sets an HttpOnly, SameSite=Lax, host-only
  browser-binding cookie on the path `/api/v1/gmail/oauth`, and the callback requires it. The browser therefore has to
  reach both the start endpoint and the redirect URI on the **same host** as the frontend: route `/api/v1/*` on the
  frontend host to the backend with a reverse proxy (as the Vite proxy does locally). A redirect URI on a different
  host fails with "invalid state".
* **HTTPS everywhere.** Google requires HTTPS redirect URIs outside localhost. With `COOKIE_SECURE=true` (or an HTTPS
  request) both the session refresh cookie and the Gmail binding cookie are `Secure`. Behind a TLS-terminating proxy,
  set Uvicorn's `--forwarded-allow-ips` to that proxy only.
* **Logs.** The callback URL carries the one-time authorization `code` and `state`. The backend replaces the query
  string of `/api/v1/gmail/oauth/callback` with `?[redacted]` in Uvicorn's access log. Any proxy, load balancer or CDN
  in front must do the same (for example, a dedicated nginx `location` for that path with `access_log off;` or a log
  format without `$args`). The callback answers with `Referrer-Policy: no-referrer` and `Cache-Control: no-store`.
  FlowJob's own Gmail logs contain error categories and exception type names only, never tokens, addresses, ids or
  email text. A failed OpenAI request (any feature) logs only the model, the exception type, the HTTP status and
  OpenAI's own error code, error type and request id, never the provider's message, the prompt, CV, job or email
  text, or the model's output.

### Scheduler, retries and recovery

* Enable `GMAIL_SCHEDULER_ENABLED=true` on the backend processes that are always running. Each process ticks every
  10 minutes (`GMAIL_SCHEDULER_TICK_SECONDS`). A MongoDB lease (`scheduler_leases`, id `gmail-scheduler`) lets only one
  process work at a time, and the per-connection lock prevents overlap with manual syncs, so several workers or
  replicas are safe. Nothing syncs while no backend process runs (scale-to-zero hosting means no background sync).
* A connection is synced every `GMAIL_SYNC_INTERVAL_HOURS` (3). A run lists at most 10 pages of 100 messages
  (metadata only) and analyses at most 4 × 50 candidates; the rest continues on the next run. AI analyses are capped
  per user (`GMAIL_DAILY_QUOTA`, 200/day) and globally (`GMAIL_GLOBAL_DAILY_QUOTA`, 2000/day), separately from Match and
  Tailoring; one analysis per email, never repeated on retry. When a daily cap is reached the run stops, the
  remaining emails stay queued, and no automatic run starts before the next UTC midnight, when the caps reset.
* **Temporary Google failures** (network errors and timeouts, Google 5xx, HTTP 429, and 403 responses whose reason is
  a rate or quota limit, on the Gmail API or the token endpoint) never ask the user to reconnect. The scheduler
  retries after 15 min, 30 min, 1 h, 2 h and 4 h, never earlier than Google's `Retry-After` (capped at 24 h); a manual
  Sync now is refused with 429 while Google's rate-limit wait is running. After five automatic retries in a row the
  connection shows "needs attention" and waits for a manual Sync now, which starts a new bounded sequence. A
  successful run clears the incident.
* **Permanent failures** need the user or operator: a revoked or expired grant (`invalid_grant` → Reconnect Gmail), a
  403/401 without a rate-limit reason (permission refused), a rejected search, or an undecryptable credential
  (configuration error).
* **Interrupted disconnects.** Disconnect first fences the connection (`status: disconnecting`), revokes the grant,
  erases the data and deletes the connection last. If the process or database fails midway, the connection stays in
  `disconnecting`: the UI shows "Disconnecting…" with **Finish disconnecting** (a safe, idempotent retry), syncing and
  reconnecting are refused meanwhile, and every scheduler tick finishes disconnects that have been pending for more
  than 5 minutes. Without the scheduler, the user's retry finishes it. A failed revocation is logged as a warning
  (category only); erasure never depends on Google confirming it.

### Key management and rotation

* `GMAIL_TOKEN_KEY` is a base64-encoded random 32-byte AES-256-GCM key, separate from `JWT_SECRET`. Generate it with
  the command in `.env.example`, keep it in the secret store, and give every backend process the same value. The
  encrypted refresh token is bound to its FlowJob user and Google account, so a copied value cannot be decrypted for
  another user.
* If the key is lost or wrong, stored credentials cannot be decrypted: Gmail shows a configuration error and each
  user must reconnect. An invalid key format disables Gmail at start-up with a log message that never contains the key.
* Rotation: set the new key as `GMAIL_TOKEN_KEY`, move the previous one into `GMAIL_TOKEN_KEYS_OLD`
  (comma-separated), and restart every process. Each credential is re-encrypted with the new key the next time it is
  used; with the scheduler on, that happens within one sync interval. Remove the old key once every connection has
  synced or reconnected.
* Rotate the Google client secret in Google Cloud by adding a new secret, deploying it, then disabling the old one.

### Privacy and data retention

Describe the following in the product's privacy notice; OpenAI processes email content on FlowJob's behalf.

**Before OpenAI.** Candidate selection uses metadata only (subject, Gmail's snippet, sender and labels), evaluated
inside the backend; other emails are never sent anywhere. Spam, Trash, Drafts, Sent and Chats are excluded.

**Sent to OpenAI** (one request per candidate email, `store=False`, no tools):
* the minimized body (at most 4,000 characters) after removing quoted replies, signatures and unsubscribe/privacy
  footers, replacing email addresses with `[email]`, phone numbers with `[phone]`, the user's own name and address
  with `[you]`, and shortening links to their host and short path segments;
* the subject (with the same redactions), the sender's display name and domain, and the received time in the user's
  time zone;
* a shortlist of up to 20 of the user's own applications (company, job title, status, date applied and known
  application references) so the model can say which one the email is about.

Not sent: attachments (never downloaded), full headers, recipients, OAuth credentials, or anyone else's data. Names of
other people that appear in the body (for example a recruiter) may remain.

**Stored while connected:** the connection (Google account id, Gmail address, encrypted refresh token, scopes, time
zone, sync progress); one ledger row per listed message (message id, thread id, received time, processing state, and
a sanitized decision with enums and counts only; no content); the validated analysis only until the message is
processed; recruitment activity (a verified excerpt of at most 300 characters, references, scheduled dates); Needs
Review items (proposal, excerpt and, for unconvertible times, the time as the email states it); dismissal records;
and suppression records for deleted Gmail-imported applications (message/thread ids, references and the normalized
employer and title). No full email body and no access token is ever stored.

**Erased on Disconnect:** all of the above, pending OAuth states, and the Gmail ids on applications. The grant is
revoked at Google (best effort). No fingerprint of dismissed or deleted items is kept.

**Kept after Disconnect:** the applications (including imported ones, marked `source: gmail` with their import time,
applied date and the time of the latest approved status decision), their manual history and the approved status
history (from/to status and the email's date, with an internal marker naming the erased review; no excerpt or Gmail
id).

**Reconnecting** re-reads the last 30 days as a new connection. Because dismissals and suppressions were erased,
suggestions the user dismissed may reappear and a deleted Gmail-imported application may be suggested again; the UI
says so in the Disconnect and Connect dialogs. Approved statuses are safe: an email older than the application's
latest status decision (manual or approved) never proposes a status change, and for a closed application a
confirmation received before its latest **approved** Gmail status decision is linked as history rather than treated
as a possible reapplication. A manual status edit only records when the user clicked, so it is not used for that
decision: such confirmations, and any confirmation with a different application reference, go to Needs Review
instead of creating a duplicate. While a connection stays connected, deleted applications are never recreated from
the same evidence.

Account deletion is not implemented in this backend; when it is added, it must run the Gmail disconnect first.

### Google OAuth Testing mode and public launch

* **Testing mode** (External, unverified): at most 100 listed test users; testers see Google's "unverified app"
  warning; refresh tokens for Gmail scopes expire after about **7 days**, so testers see "Reconnect Gmail" weekly
  (reconnecting with the same account keeps all progress).
* **Public launch:** `gmail.readonly` is a Google *restricted* scope. Publishing requires Google's OAuth app
  verification (verified domains, homepage and privacy policy, a scope justification and a demo video) and, because
  data derived from Gmail is stored on FlowJob's servers, a third-party security assessment (CASA) by an approved
  assessor, renewed periodically. The app must also comply with the Google API Services User Data Policy, including
  its Limited Use requirements (for example, no use for advertising and no human reading except as that policy
  allows), and disclose the transfer to OpenAI. Check Google's current requirements before applying; this MVP has not
  been verified or assessed.
* Narrower scopes do not fit: `gmail.metadata` cannot read message bodies, which the confirmation and interview
  detection needs.

### Gmail behaviour in tests

Tests replace Google (OAuth, ID tokens and the Gmail API) and the model with deterministic fakes
(`tests/gmail_fakes.py`, `tests/test_gmail_pipeline.py`). No test reaches Google or OpenAI. A mocked OAuth
callback is not proof that a real Google client is configured.

## Known limits

- **AI output is checked, not proven true.** The backend verifies tailoring targets, reorder permutations,
  and that an added skill is named in a verbatim quotation from the CV. It cannot prove that a rewritten
  sentence adds no new fact, so rewrites always need the user's approval and the truthfulness confirmation.
  In matching, quotations are verified word for word, but whether a quotation is relevant evidence is left
  to the prompt.
- **Profile data has two shapes.** `Profile.frontend_data` holds what the browser edits and is the source of
  truth for those fields. Edits through the older native profile routes do not update it automatically.
- **Production hardening is still needed.** Before a public deployment, review HTTPS and cookie settings,
  allowed origins, routing, login abuse protection, and cookie/CSRF behaviour for the chosen host.

## Tailoring evaluation script

`scripts/tailor_eval/` measures tailoring quality, time and cost on fixed invented test cases (see its
README). It is not part of the test suite. **It calls OpenAI for real when a real key is configured, which
costs money, and it creates throwaway accounts in whatever database the backend uses.** Only run it against
a development database, and only when a paid run has been agreed.
