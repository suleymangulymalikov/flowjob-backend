# Backend instructions

Read PROJECT_HANDOFF.md and GITHUB_COMPARISON.md for the preserved source baseline and project constraints.

- Target repository: suleymangulymalikov/flowjob-backend; existing branch feature/frontend-integration. Preserve its history and teammate changes. No push/merge without approval; no force push/reset/history deletion.
- FastAPI, Pydantic, Beanie and MongoDB only. Browser routes use /api/v1; legacy routes remain supported. No PostgreSQL.
- Preserve app/cv/templates/harvard.html. Both saved-CV PDFs and preview PDFs use this original template.
- Tailoring creates TailorDraft suggestions and a NEW SavedCv on acceptance. Preserve original CV data and sourceCvId. Never fabricate qualifications; prompts and original-text matching alone cannot prove suggested text is factual.
- No paid OpenAI calls without approval. Override OPENAI_API_KEY=mock during tests; keep credentials out of output and Git.
- Run backend tests only with the test-owned MongoDB launcher: `env -u MONGODB_URI -u MONGODB_DB_NAME FLOWJOB_TEST_MONGOD=mongod uv run --locked pytest`. See README.md. The launcher verifies a private local process before app imports, creates a unique database and forces mock OpenAI. Never substitute an existing URI or database.
- Use Python 3.12+, uv sync --locked, and uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000. WeasyPrint needs system Pango libraries.
- Ignore all .env variants except sanitized .env.example. Inspect staged paths and content for secrets before any commit.
