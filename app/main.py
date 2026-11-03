from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import client, init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await client.close()


app = FastAPI(title="FlowJob API", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
