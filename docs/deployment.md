# Deployment

FlowJob runs as three parts:

| Part | Host | Config in repo |
| --- | --- | --- |
| Frontend (React) | Vercel | `flowjob-frontend/vercel.json` |
| Backend (FastAPI) | Render, Docker | `Dockerfile`, `render.yaml` |
| Database | MongoDB Atlas | connection string in Render only |

The browser only ever talks to the Vercel address. Vercel forwards `/api/v1/*` to the Render backend, so the
frontend and API share one host: the login cookie and the Gmail OAuth flow need that (see `docs/gmail.md`).

## Updating the live site

Merge into `main`. Vercel rebuilds the frontend on every push to `main`. Render redeploys the backend after the
GitHub checks (the test suite) pass on `main` (`autoDeployTrigger: checksPass`), so a red build never goes live.

## First-time setup

1. **Atlas:** Network Access must allow `0.0.0.0/0` (Render's free plan has no fixed IP). The database user needs
   read/write on the `flowjob` database and permission to create indexes.
2. **Render:** New > Blueprint > this repository. Fill in `MONGODB_URI`, `OPENAI_API_KEY` and `FRONTEND_BASE_URL`
   (the Vercel address). `JWT_SECRET` is generated. Check `https://<service>.onrender.com/health` says `ok`.
3. **Vercel:** Add New > Project > the frontend repository. Vercel reads `vercel.json`; no environment variables
   are needed (never put secrets in `VITE_*`). If Render named the service differently, change the backend
   address in `vercel.json`.

## Behaviour to know

- The free Render plan sleeps after 15 minutes without traffic; the first request after that waits about a
  minute (Vercel waits up to 120 s). A paid instance avoids this.
- The backend runs Uvicorn with `--proxy-headers --forwarded-allow-ips='*'` so login and registration limits count
  per visitor. Vercel overwrites `X-Forwarded-For` with the real client address. Someone calling the Render
  address directly can set that header themselves, so these limits are a courtesy, not a security boundary there.
- Gmail stays unavailable until the Google OAuth settings and `GMAIL_TOKEN_KEY` are added (README, "Gmail
  integration"), with the redirect URI on the Vercel host.
- Mock AI is refused when `COOKIE_SECURE=true`, so a deployment always uses the real OpenAI key.
