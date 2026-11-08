# Backend instructions

Read PROJECT_HANDOFF.md and GITHUB_COMPARISON.md for the preserved source baseline and project constraints.

- Target repository: suleymangulymalikov/flowjob-backend; existing branch feature/frontend-integration. Preserve its history and teammate changes. No push/merge without approval; no force push/reset/history deletion.
- FastAPI, Pydantic, Beanie and MongoDB only. Browser routes use /api/v1; legacy routes remain supported. No PostgreSQL.
- Preserve app/cv/templates/harvard.html. Both saved-CV PDFs and preview PDFs use this original template.
- Tailoring creates TailorDraft suggestions and a NEW SavedCv on acceptance. Preserve original CV data and sourceCvId. Never fabricate qualifications; prompts and original-text matching alone cannot prove suggested text is factual.
- No paid OpenAI calls without approval. Override OPENAI_API_KEY=mock during tests; keep credentials out of output and Git.
- tests/conftest.py has an autouse collection-deletion fixture and hardcodes flowjob_test. NEVER invoke pytest using the existing .env MongoDB connection. Start a disposable local mongod with a fresh directory and port, explicitly set MONGODB_URI to that loopback port, and use a test-only JWT_SECRET. Verify the instance is yours before running or shutting it down.
- Use Python 3.12+, uv sync --locked, and uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000. WeasyPrint needs system Pango libraries.
- Ignore all .env variants except sanitized .env.example. Inspect staged paths and content for secrets before any commit.
