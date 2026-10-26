from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.core.config import ConfigurationError
from app.db.health import check_database
from app.db.session import get_engine

app = FastAPI(title="FlowJob API")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/db")
def health_db():
    try:
        result = check_database(get_engine())
    except ConfigurationError as exc:
        return JSONResponse(status_code=503, content={"status": "unavailable", "detail": str(exc)})
    if not result.ok:
        return JSONResponse(status_code=503, content={"status": "unavailable", "detail": result.detail})
    return {"status": "ok", "detail": result.detail}
