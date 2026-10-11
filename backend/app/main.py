from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text

from app.api import router
from app.admin_api import router as admin_router
from app.analytics_api import router as analytics_router
from app.assistant_api import router as assistant_router
from app.maintenance_api import router as maintenance_router
from app.browser_api import router as browser_router
from app.browser_internal import router as browser_internal_router
from app.browser_sessions import login_manager
from app.config import get_settings
from app.db import session_factory
from app.notification_api import router as notification_router
from app.sentiment_api import router as sentiment_router
from app.news_api import router as news_router
from app.recruitment_management_api import router as recruitment_management_router
from app.retention_api import router as retention_router
from app.security import bootstrap_admin
from app.probe_api import router as probe_router
from app.model_settings import router as model_settings_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    with session_factory()() as db:
        bootstrap_admin(db)
    yield
    login_manager.close_all()


app = FastAPI(title="观势 · 企业情报 API", version="0.1.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Pydantic's default payload contains original input, which can include secrets.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
        for item in exc.errors()
    ]})


app.add_middleware(CORSMiddleware, allow_origins=list(filter(None, [
    get_settings().app_url, get_settings().probe_public_url,
])),
                   allow_credentials=True, allow_methods=["*"], allow_headers=["Content-Type", "Idempotency-Key"])


@app.middleware("http")
async def origin_guard(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in {
            get_settings().app_url.rstrip("/"), get_settings().probe_public_url.rstrip("/"),
        }:
            return JSONResponse({"detail": "不允许的请求来源"}, status_code=403)
    return await call_next(request)


app.include_router(router)
app.include_router(admin_router)
app.include_router(analytics_router)
app.include_router(assistant_router)
app.include_router(maintenance_router)
app.include_router(browser_router)
app.include_router(browser_internal_router)
app.include_router(notification_router)
app.include_router(sentiment_router)
app.include_router(news_router)
app.include_router(recruitment_management_router)
app.include_router(retention_router)
app.include_router(probe_router)
app.include_router(model_settings_router)


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/api/v1/readiness")
def readiness():
    try:
        with session_factory()() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception:
        return JSONResponse({"status": "not_ready"}, status_code=503)
