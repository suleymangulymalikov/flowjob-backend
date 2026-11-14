import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.applications.router import router as applications_router
from app.matches.router import router as matches_router
from app.tailor.router import router as tailor_router
from app.auth.router import router as auth_router
from app.cv.router import router as cv_router
from app.integration.router import router as ui_router
from app.db import client, init_db
from app.gmail.router import router as gmail_router
from app.profiles.router import router as profiles_router
from app.users.router import router as users_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    from app.config import settings
    from app.gmail.scheduler import scheduler
    if settings.gmail_scheduler_enabled:
        scheduler.start()
    from app.gmail import crypto, sync
    if settings.gmail_configured and crypto.key_problem():
        # Never log the key itself; Gmail stays unavailable until the configuration is fixed.
        logging.getLogger('app.gmail').error('Gmail is disabled: %s', crypto.key_problem())
    try:
        yield
    finally:
        if settings.gmail_scheduler_enabled:
            await scheduler.stop()
        await sync.shutdown()  # cancel in-flight manual syncs before the database client closes
        await client.close()


app = FastAPI(title="FlowJob API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(ui_router)
app.include_router(applications_router)
app.include_router(matches_router)
app.include_router(tailor_router)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(profiles_router)
app.include_router(cv_router)
app.include_router(gmail_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
