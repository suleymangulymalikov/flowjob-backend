from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.auth.router import router as auth_router
from app.db import client, init_db
from app.users.router import router as users_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await client.close()


app = FastAPI(title="FlowJob API", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(users_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
