# Operations and limits

Reference for running the FlowJob backend: AI usage limits, CV and PDF limits, and authentication settings for
deployment. For setup, see the [README](../README.md).

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
carries forward any usage that older versions recorded for that day.

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

## Smart CV Import

`POST /api/v1/cvs/import` organizes an uploaded PDF CV into the CV Builder's structure for the user to review. It
stores nothing: the PDF, its text and the result exist only during the request. The reviewed CV is saved later,
only through `POST /api/v1/cvs`.

**Request.** The body is the PDF itself with `Content-Type: application/pdf`. The request must carry the header
`X-FlowJob-AI-Consent: cv-import`, which the app sends only after the user agrees that the CV text is sent to
OpenAI to organize it; without it the API answers 428. The user must be signed in.

**File limits.** At most 5 MB (413, checked before the body is read), 10 pages and 30,000 characters of text. The
file must start with the PDF header; its declared name or type is not trusted. Password-protected, unreadable,
too complex and scanned (no selectable text) PDFs return 422 with a message that says which. Scanned PDFs are not
supported yet; OCR can be added later as another extraction method without changing the endpoint.

**Isolated parsing.** pypdf runs in a separate Python process (`app/cv_import/worker.py`) started with `-I`, a
minimal environment without credentials, a 10-second CPU limit, a 768 MB memory limit (enforced on Linux), no
permission to write files, and a 15-second wall-clock timeout after which it is killed. At most two PDFs are parsed
at a time per backend process; more return 503.

**AI call and quota.** After the text is extracted, one `import` quota unit is reserved (`IMPORT_DAILY_QUOTA`,
default 5 per user per UTC day, also counted toward `AI_GLOBAL_DAILY_QUOTA`), and one gpt-4.1 call sorts the text
into fields (`IMPORT_MAX_OUTPUT_TOKENS` 8000, `IMPORT_TIMEOUT_SECONDS` 90). A failed call refunds the unit; a file
rejected before the call never uses one. Typical cost is about $0.02–0.04 per import at the default prices.

**Checks against the PDF text.** The model is told to copy values exactly, and the backend keeps only values it
finds in the extracted text: names, contact details, job titles, employers, schools, degrees, dates, bullet points,
projects, skills and languages. Matching tolerates differences that come from PDF extraction (line breaks,
whitespace, ligatures, curly quotes, dash styles, bullet symbols, words hyphenated across a line break) but not
changed wording, numbers or capitalization, and words must match whole. Dates must be written next to their own
entry and bullet points under their own entry, so nothing moves between jobs. A phone number is returned as the CV
writes it. Only unambiguous month formats such as `Jan 2020` or `01/2020` are converted to `2020-01`; anything else
is kept as written, and `Present` marks an ongoing entry. Every value left out is listed in `warnings` with a
reason, and empty fields are listed in `missing`.

**Response.** `{content, warnings, missing, source: {pages, method}, model}`, where `content` has the same shape as
`CvContent` and can be sent to `POST /api/v1/cvs` after review. The response is marked `Cache-Control: no-store`.
Logs contain only fixed error codes, never CV text.

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
