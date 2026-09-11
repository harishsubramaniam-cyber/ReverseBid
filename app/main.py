from __future__ import annotations

import asyncio
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from fastapi import Depends

from . import config, mailer, migrate, scheduler
from .db import Base, SessionLocal, engine
from .routers import (assistant, attachments, auctions, auth, awards, bidding, dashboard,
                      masters, messages, notifications, reports)
from .security import csrf_protect, current_user_optional
from .web import render


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    added = migrate.run()
    if added:
        print("Database updated with new columns:", ", ".join(added))
    # A demonstration deployment: if the database is empty, fill it with the
    # sample company so whoever opens the link has something to click. Real
    # installations never set this, and it does nothing once there is data.
    adopted = migrate.adopt_into_one_organisation()
    if adopted:
        print(f"This installation predates organisations, so {adopted} "
              "have been moved into one.")
    if config.DEMO_SEED:
        from .models import User as _User
        probe = SessionLocal()
        try:
            empty = probe.query(_User).count() == 0
        finally:
            probe.close()
        if empty:
            try:
                import seed as _seed
                _seed.build()
                print("Demo data created. Sign in as buyer@demo.in / demo1234.")
            except Exception as exc:            # pragma: no cover - never fatal
                print(f"Could not create the demo data: {type(exc).__name__}: {exc}")
    # Anything left queued or failed by a previous run goes out now: the send
    # queue only lives in memory, so a restart is also the retry.
    waiting = mailer.requeue_pending()
    if waiting:
        print(f"Retrying {waiting} email(s) left over from the last run.")
    task = asyncio.create_task(scheduler.run_forever())
    yield
    task.cancel()


app = FastAPI(title=f"{config.APP_NAME} — reverse auction platform", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "app" / "static")), name="static")

#: Every route goes through the CSRF check. It does nothing for GET, and for
#: anything that changes data it insists the request came from a page this app
#: rendered - otherwise another site could post to it using someone's cookie.
for router in (auth.router, dashboard.router, masters.router, auctions.router, bidding.router,
               awards.router, attachments.router, messages.router, reports.router,
               notifications.router, assistant.router):
    app.include_router(router, dependencies=[Depends(csrf_protect)])


#: Plain-language wording for anything that reaches the error screen.
FRIENDLY = {
    400: ("We could not do that", "Something in the request was not right."),
    403: ("You do not have access to that", "Your account cannot open this page."),
    404: ("We could not find that page", "The link may be old, or the item may have been "
          "deleted."),
    405: ("That does not work from here", "The page was reached in a way it does not support. "
          "Go back and try the button again."),
    413: ("That was too large", "Try again with something smaller."),
    422: ("Some details were not right", "Please check the form and try again."),
    500: ("Something went wrong at our end", "The problem has been logged. Nothing you did "
          "caused it."),
}


def _wants_html(request: Request) -> bool:
    """A browser navigating gets a page; fetch() and API calls get JSON.

    Browsers put ``text/html`` in Accept when they navigate, and ``*/*`` when
    JavaScript asks — which is exactly the distinction we want.
    """
    return "text/html" in request.headers.get("accept", "")


def _error_screen(request: Request, code: int, detail: str = ""):
    title, fallback = FRIENDLY.get(code, ("Something went wrong", "Please try again."))
    message = detail if isinstance(detail, str) and detail else fallback
    # Keep the navigation on the page: being lost is bad enough without also
    # losing the menu. The session lookup needs its own short-lived db handle.
    user = None
    db = SessionLocal()
    try:
        user = current_user_optional(request, db)
        return render(request, "error.html",
                      {"code": code, "title": title, "message": message},
                      user=user, db=db if user else None, status_code=code)
    finally:
        db.close()


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 401:
        if _wants_html(request):
            return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
        return JSONResponse({"error": "Please sign in again."}, status_code=401)
    if _wants_html(request):
        return _error_screen(request, exc.status_code, exc.detail)
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    """A field arrived missing or in the wrong shape - usually a half-filled form."""
    if _wants_html(request):
        return _error_screen(
            request, 422,
            "One of the boxes was empty or held something unexpected. Go back, fill it in, "
            "and submit again.")
    return JSONResponse({"error": "Some fields were missing or invalid."}, status_code=422)


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    """Nothing reaches the person as a stack trace."""
    traceback.print_exception(type(exc), exc, exc.__traceback__)
    if _wants_html(request):
        return _error_screen(request, 500)
    return JSONResponse({"error": "Something went wrong at our end."}, status_code=500)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok", "email_mode": "smtp" if config.EMAIL_ENABLED else "outbox"}
