# Backend instructions

Read README.md first: it covers setup, tests, limits and the Gmail integration.

- Repository: suleymangulymalikov/flowjob-backend. Work on a branch from `main` and open a pull request; CI must pass. Preserve history and teammate changes. No push/merge without approval; no force push/reset/history deletion.
- FastAPI, Pydantic, Beanie and MongoDB only. Browser routes use /api/v1; legacy routes remain supported. No PostgreSQL.
- Preserve app/cv/templates/harvard.html. Both saved-CV PDFs and preview PDFs use this original template.
- Job Matching is AI-first (`app/ai_matching`): the model extracts and cites, the backend verifies quotes/citations and scores deterministically; never let model output set a score or category unchecked. Tailoring creates TailorDraft suggestions (typed, path-addressed, validated in `app/tailor/grounding.py`; only verified reorders auto-preview) and a NEW SavedCv on acceptance. Preserve original CV data and sourceCvId. Never fabricate qualifications; verification cannot prove a rewrite is factual, so rewrites, deletions and new skills need explicit user approval. Stored prompt/cache version strings (`MATCH_PROMPT_VERSION`, `TAILORING_PROMPT_VERSION`, `AI_FIRST_ANALYSIS_VERSION`, `GMAIL_PROMPT_VERSION`) are cache identities: change them deliberately, because a change re-analyses cached results.
- Gmail (`app/gmail`) is read-only and optional. The model only describes an email; `reconcile.py` decides every write. Existing application statuses change only through an approved review; never store full email bodies or access tokens; keep refresh tokens encrypted (`crypto.py`) and Google calls behind `google.api()` so tests use fakes. Sync writes must stay behind the fenced connection lock. No live Gmail access or paid classification without approval.
- No paid OpenAI calls without approval; `scripts/tailor_eval` makes real calls. The test runner forces OPENAI_API_KEY=mock; keep credentials out of output and Git.
- Run backend tests only with the test-owned MongoDB launcher: `env -u MONGODB_URI -u MONGODB_DB_NAME FLOWJOB_TEST_MONGOD=mongod uv run --locked pytest`. See README.md. The launcher verifies a private local process before app imports, creates a unique database and forces mock OpenAI. Never substitute an existing URI or database.
- Use Python 3.12+, uv sync --locked, and uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000. WeasyPrint needs system Pango libraries.
- Ignore all .env variants except sanitized .env.example. Inspect staged paths and content for secrets before any commit.
