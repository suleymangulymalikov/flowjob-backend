# FlowJob project handoff

This is the record from the preserved two-project export. Paths and pre-restoration status below describe that export.

Verified 2026-11-08 (Europe/Warsaw). Scope: preserve and inspect existing work; no application features implemented, commits created, pushes or merges performed.

## Readiness

Safe for controlled local development with the safeguards below. NOT yet a version-controlled working checkout or a production-readiness certification. The supplied root, backend and frontend have no `.git` metadata. No current local branch, remotes, worktrees, commit ancestry or unpushed commits can therefore be recovered from this export. A search under `/Volumes/DevSSD/Desktop/FinalProject` found no other Git repositories before preservation mirrors were created. Any Claude-era local commits not on GitHub require the original checkout or a separate backup to recover; source content is preserved here.

## Preservation and GitHub state

Local-only preservation artifacts in `.handoff/`:

- `source-before-handoff.tar.gz`: 127 source/config-example/documentation files captured before edits; every archived file verified against `source-manifest.json` SHA-256 hashes.
- `github/backend.git` and `github/frontend.git`: full read-only mirror downloads of GitHub's advertised histories/refs. These are bare backups, NOT development worktrees. Never push from a mirror repository.
- Test logs, comparison intermediate report and isolated local test database files.

Private `.env`/`.env.local` files remain untouched in their original locations and were deliberately excluded from the source archive. Dependencies, generated builds, caches and OS metadata were also excluded. This same-disk snapshot protects against editing mistakes, not disk failure; retain a separate approved secure backup of source and separately managed secrets. Do not upload `.handoff/` wholesale.

Verified remote branches at inspection:

| Repository | Branch | Tip |
| --- | --- | --- |
| suleymangulymalikov/flowjob-backend | feature/frontend-integration | b8448a1471876a1b54e23be29cc3300ef300459e |
| backend | main | ae3c5fdb396ef839fc1be8c0a6cfb48564799af9 |
| backend | BE-002-database-foundation | 51bef15fcdc5dcaa894f010a2c0fe8b0d1c5a002 |
| Youssif-El-menshawy/flowjob-frontend | feature/backend-integration | f18037224e50512db557aa97a56698d3cdead3de |
| frontend | feature/frontend-mvp | 73e7075ad2562ca0a8c8d72bbd98392738c059a5 |
| frontend | main | d84305e07b16d7adf1d848c7e879deca78bb7cd8 |

Canonical URLs:
- https://github.com/suleymangulymalikov/flowjob-backend
- https://github.com/Youssif-El-menshawy/flowjob-frontend

See `GITHUB_COMPARISON.md` for complete file lists against ALL six branches. Against the relevant integration tips, the initial backend has **19 local-only and 11 differing files**, frontend **9 local-only and 19 differing files**. Backend additions include applications, matching, AI services, tailoring and their tests. Frontend changes include these flows, account menu, dashboard/profile handling and Harvard PDF preview lifecycle/tests. Different files require review; a byte difference alone does not establish which side is newer. The frontend remote tracks `tsconfig.tsbuildinfo`; this generated file exists locally but was excluded from comparison/source archive. No actual source files are missing locally relative to the integration branches.

The obsolete backend database-foundation branch contains PostgreSQL/Alembic files. Preserve that branch's history but DO NOT import its architecture into this MongoDB application.

## Verified structure and architecture

- Root: exported two-project workspace, `README-INTEGRATION.md`, ignore rules and this handoff. Root integration README is stale; use this document for current capabilities and safe testing.
- `frontend/`: React 18 + TypeScript + Vite 6, React Router, TanStack React Query, React Hook Form/Zod, Tailwind 4. `src/app` owns routing/layout; `src/features` contains auth, landing, dashboard, profile, CVs, applications, match/tailor and settings. `src/shared/api` defines typed contracts, hooks and fetch client. `src/mocks` contains MSW/demo implementations, not production persistence. `public` contains static assets/service worker; `.github/workflows/ci.yml` runs frontend checks on Node 22.
- `backend/`: Python 3.12+, FastAPI, Pydantic Settings, Beanie documents backed by PyMongo AsyncMongoClient and MongoDB. `app/main.py` initializes MongoDB on startup and includes both legacy and browser routes. `app/config.py` reads backend `.env`. Models: User, RefreshToken, Profile, SavedCv, Application, MatchRecord, MatchUsage and TailorDraft. `uv.lock` locks dependencies. No PostgreSQL dependency/architecture is introduced here.
- Browser API: `/api/v1`; Vite proxies it to `http://localhost:8000`. Access token stays in browser memory; rotating refresh token uses an HttpOnly cookie. Development CORS allows localhost/127.0.0.1 port 5173. Legacy `/auth`, `/users`, `/profile`, `/cv/pdf` remain.
- PDFs: Jinja + WeasyPrint, original `backend/app/cv/templates/harvard.html`; frontend React-PDF/PDF.js viewer. Saved-CV PDF and unsaved preview endpoints render the same template. Renderer tries progressively smaller sizes/margins and may return multiple pages for long content.
- AI: OpenAI SDK services isolated under `app/services` (matching pipeline in `app/ai_matching`, tailoring validation in `app/tailor/grounding.py`); structured matching/tailoring responses, retries/timeouts, persisted results, quota counters and cache reuse. `OPENAI_API_KEY=mock` takes a local canned path. Empty key disables real calls; real credentials permit paid calls and MUST NOT be exercised without approval.

Harvard template SHA-256: `fd5a2224f2c032db252966350d2132d60ca4cafef52596b71a81ddf5f36132b5`. Byte-identical to the backend integration branch; unchanged by this handoff.

## Implemented capabilities verified in source and tests

- Browser registration/login/refresh/logout; profile persistence and legacy mapping; account name/email and password updates.
- Owned saved-CV CRUD; Harvard PDF download and preview; original/tailored CV metadata and provenance.
- Applications CRUD with validation, filtering and linked CV ownership checks.
- AI job matching (AI-first, `app/ai_matching`): the model extracts professional qualifications (excluding working conditions, duties and generic traits), classifies them Required/Standard/Preferred and cites the CV; the backend verifies job quotes and CV citations and computes the score deterministically (shares 60/25/15, credit 100/50/0%). Result history, provider error mapping, cache and per-user quotas. Historical reports stay readable; the older weighted scorer (`app/matches/scoring.py`) is retained for reference only.
- AI tailoring: source CV plus match context (qualifications, verified statuses, cited evidence) or the raw job post; typed suggestions (rewrite, reorder, delete, add skill) addressed by CV path and validated server-side (targets, exact reorders, evidence-backed skill additions; rejections counted in `proposalStats`). Only verified reorders are auto-previewed (Undo); rewrites, deletions and new skills need explicit approval, are editable, and live preview, download and save work with pending or zero suggestions. Truthfulness confirmation remains. Acceptance uses POST `/cvs` to create an independent tailored CV with `sourceCvId`; the source remains intact. Tests cover original preservation and tailored CV survival after source deletion.
- Frontend dashboard/account menu, profile and application flows, match/tailor screens, preview concurrency/debounce behavior and navigation/layout tests.

“Implemented” does not mean live Atlas/OpenAI or deployed end-to-end behavior was tested. AI tests use mocks.

## Development commands

Use Python 3.12+ and uv; Node 22 matches CI (installed Node 24.13.0/npm 11.6.2 passed this audit). WeasyPrint requires native Pango libraries (`brew install pango` on macOS). Review lockfiles before dependency changes.

Backend, from `backend/`:

```sh
uv sync --locked
# Only for a new checkout: create .env from the sanitized example; never overwrite existing .env.
# Set MONGODB_URI, MONGODB_DB_NAME and a private JWT_SECRET securely.
# Keep OPENAI_API_KEY=mock for unpaid development unless explicitly approved otherwise.
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Frontend, from `frontend/`:

```sh
npm ci
# Existing .env.local is retained. Connected mode: VITE_USE_MOCKS=false.
# Leave VITE_API_URL unset to use /api/v1 and the development proxy.
npm run dev
npm test
npm run lint
npm run typecheck
npm run build
```

Frontend: http://localhost:5173; backend docs: http://localhost:8000/docs; health: http://localhost:8000/health. Startup requires reachable MongoDB even for health checks. Production needs its own `/api/v1` proxy; Vite's development proxy is not production hosting.

## Historical test results

For current backend testing, see [README.md](README.md#tests). The restored backend now requires pytest to start its own disposable MongoDB.

Audit results:

| Check | Result |
| --- | --- |
| Frontend Vitest | 39 passed, 7 files |
| Frontend ESLint | Passed |
| Frontend TypeScript | Passed |
| Frontend production build | Passed |
| Backend pytest | 93 passed in 10.91 s |

Frontend tests emitted React Router future notices and jsdom `window.scrollTo` not-implemented stderr; checks still passed. Logs are under `.handoff/`. Initial sandbox restrictions blocked local MongoDB socket/connect access; rerunning with authorized local access succeeded.

For the original handoff audit, the backend tests used a disposable MongoDB 8.0.10 with a fresh data directory and mocked OpenAI. That instance has been stopped. No Atlas data was touched and no paid AI requests were made.

## Secret handling verification

- Existing backend `.env` and frontend `.env.local` are present and were not printed or modified. No credential values appear in this handoff.
- Root and standalone repository ignore files now cover `.env`, `.env.*` (except `.env.example`), private key files and `.handoff/`; frontend also ignores `*.tsbuildinfo`.
- `git check-ignore --no-index` using a disposable bare audit repository verified the rules independently for root/backend/frontend, including `.env.production.local`. Examples remain eligible for tracking.
- Neither downloaded repository has private `.env` paths in reachable history. Exact current configured secret values were checked against the source snapshot and all reachable historical blobs: no matches. A source heuristic scan for OpenAI keys, credential-bearing MongoDB URIs and private-key headers found none outside private env files.
- This is not proof that every possible historical secret format is absent. Ignore rules never untrack already tracked files; inspect the staged index again in restored checkouts before committing. No active index exists in the supplied export.

## Known issues and limits

1. Git metadata is missing from the export. Recover source into existing histories via fresh checkouts; do not invent ancestry or initialize replacement repositories over these folders.
2. Root integration README incorrectly says applications/matching/tailoring are unimplemented and saved-CV export is browser-only. The original export's backend test command was unsafe with shared Atlas configuration. The restored backend uses the isolated launcher documented in README.md.
3. Gmail connect/sync, password recovery, account export/deletion, application suggestion-review/undo hooks have frontend paths but no matching real backend implementations. Mock handlers are not evidence of completion.
4. AI factuality is not guaranteed. The backend verifies tailoring targets, reorder permutations and that an added skill is named in a verbatim CV quotation, but it cannot verify that a rewrite adds no new fact; rewrites are prompt-constrained and always need the user's approval and the truthfulness confirmation. Evidence relevance in matching is likewise prompt-enforced on top of verbatim citation checks. Never fabricate qualifications.
5. Tailoring enforces an AI global daily quota, but matching's reservation currently only enforces its own per-user quota. The stated global cap does not cover all matching calls.
6. `Profile.frontend_data` is authoritative for browser fields; legacy profile edits do not automatically synchronize it. Production hardening still needs appropriate HTTPS/cookie settings, origins, deployment routing, login abuse controls and hosting-specific cookie/CSRF review.
7. No real Atlas/OpenAI integration, production deployment, full browser journey or visual PDF layout review was performed. Existing PDF tests passed and original template was preserved.

## Safe commit recommendation (not executed)

1. Retain this export and backup. Clone both canonical GitHub repositories into NEW empty sibling directories; do not clone over the current folders. Fetch current remote refs.
2. Check out tracking branches `feature/frontend-integration` (backend) and `feature/backend-integration` (frontend). Their tips above are the review baselines, not guaranteed future tips. Preserve all new teammate commits.
3. Compare current remote tips with these recorded tips. For files changed upstream since inspection, perform a three-way comparison using the recorded GitHub version, latest upstream and preserved local file. Do not blindly overwrite or use a delete-sync operation. If actual merge/conflict work is required, prepare it and obtain the user's approval before merging.
4. Apply only the reviewed source additions/changes in `GITHUB_COMPARISON.md`, plus relevant AGENTS.md/ignore updates. Carry this handoff and comparison into an agreed tracked documentation location in each repository, since the export root is not versioned. Keep all existing remote-only source files until explicitly reviewed. Ignore generated build info; removing an already tracked build artifact should be a separate reviewed maintenance change.
5. Re-run checks with the isolated test procedure. Inspect `git status`, `git diff --check`, and staged diffs. Stage explicit paths, never an unreviewed blanket add. Verify `.env` exclusions and scan staged content for secrets. Compare the Harvard hash and original-preservation tests.
6. Suggested local commits: backend applications; backend matching/tailoring + associated configuration/models/tests; frontend connected flows/preview/profile changes + tests; handoff/ignore documentation. Keep dependencies between these changes explicit and verify each intended commit remains coherent. A single reviewed preservation commit per repository is also reasonable if splitting would break the existing integrated state.
7. Before proposing a push, fetch again and check whether each remote branch advanced. Never reset away teammate commits or force-push. Present the exact commits and target branch for user approval; push or merge only after that approval. A normal push rejection means stop and reconcile, not overwrite.

## Files changed by this handoff

Created PROJECT_HANDOFF.md, GITHUB_COMPARISON.md, root AGENTS.md and backend/frontend AGENTS.md. Tightened the three .gitignore files. Existing application source and Harvard template were not edited; original source hash verification confirms only the three ignore files changed among the 127 captured files. Test/build outputs were regenerated locally and are ignored.
