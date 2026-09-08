from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config, migrate, scheduler
from .db import Base, engine
from .routers import (approvals, assistant, auctions, auth, awards, bidding, dashboard,
                      masters, messages, notifications, reports)
from .web import render


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    added = migrate.run()
    if added:
        print("Database updated with new columns:", ", ".join(added))
    task = asyncio.create_task(scheduler.run_forever())
    yield
    task.cancel()


app = FastAPI(title=f"{config.APP_NAME} — reverse auction platform", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "app" / "static")), name="static")

for router in (auth.router, dashboard.router, masters.router, auctions.router, bidding.router,
               awards.router, approvals.router, messages.router, reports.router,
               notifications.router, assistant.router):
    app.include_router(router)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    wants_html = "text/html" in request.headers.get("accept", "")
    if exc.status_code == 401:
        if wants_html:
            return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
        return JSONResponse({"error": "Please sign in again."}, status_code=401)
    if wants_html and exc.status_code in (403, 404):
        message = exc.detail if isinstance(exc.detail, str) else "Something went missing."
        return render(request, "error.html",
                      {"code": exc.status_code, "message": message},
                      status_code=exc.status_code)
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok", "email_mode": "smtp" if config.EMAIL_ENABLED else "outbox"}
