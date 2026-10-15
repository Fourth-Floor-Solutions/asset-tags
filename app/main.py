"""TagDesk: issue, print and look up client-branded asset tags.

This module builds the app: middleware, error handlers and the routers in
app/routes. Shared page helpers are in app/web.py; tag logic used by
several areas is in app/tagops.py.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import auth, db, hudu, hudu_archive, labels, log, settings
from .routes import clients, coverage, login, printing, public, tags, users
from .routes import settings as settings_routes
from .web import HERE, PUBLIC_BASE_URL, page


def _session_secret() -> str:
    if os.environ.get("SESSION_SECRET"):
        return os.environ["SESSION_SECRET"]
    path = os.path.join(db.DATA_DIR, "session_secret")
    os.makedirs(db.DATA_DIR, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()


log.setup()
db.init()
auth.seed_admin()
labels.apply_overrides(settings.formats())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    hudu_archive.start()  # background check for devices archived in Hudu
    yield
    hudu_archive.stop()


app = FastAPI(title="TagDesk", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

SETUP_EXEMPT = ("/setup", "/static/", "/favicon", "/login", "/logout", "/auth/", "/healthz", "/version", "/changelog")


CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _origin_ok(request: Request) -> bool:
    """Reject cross-site form posts: the Origin (or Referer) must be this site."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin or origin == "null":
        return origin is None  # no header at all: non-browser client
    allowed = {urlparse(PUBLIC_BASE_URL).netloc.lower(), (request.headers.get("host") or "").lower()}
    return urlparse(origin).netloc.lower() in allowed


@app.middleware("http")
async def security(request: Request, call_next):
    if request.method not in SAFE_METHODS and not _origin_ok(request):
        response = PlainTextResponse("This request came from another site and was refused.", status_code=403)
    else:
        response = await call_next(request)
    for k, val in SECURITY_HEADERS.items():
        response.headers.setdefault(k, val)
    return response


@app.middleware("http")
async def load_user(request: Request, call_next):
    with settings.request_scope():
        return await _load_user(request, call_next)


async def _load_user(request: Request, call_next):
    request.state.user = auth.session_user(request)
    user = request.state.user
    # A new install sends its first admin to the setup screen until it's saved.
    if (
        user
        and user["role"] == "admin"
        and request.method == "GET"
        and not request.url.path.startswith(SETUP_EXEMPT)
        and not settings.setup_done()
    ):
        return RedirectResponse("/setup", status_code=303)
    return await call_next(request)


app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret(),
    session_cookie="tagdesk_session",
    max_age=60 * 60 * 12,
    same_site="lax",
    https_only=PUBLIC_BASE_URL.startswith("https") and os.environ.get("COOKIE_INSECURE") != "1",
)


@app.exception_handler(auth.LoginRequired)
async def _login_required(request: Request, exc):
    return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)


@app.exception_handler(hudu.HuduOff)
async def _hudu_off(request: Request, exc):
    """Hudu features are only available while Hudu is set up."""
    msg = "Hudu isn't set up. An admin can add its address and API key in Settings."
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": msg}, status_code=404)
    return page(request, "message.html", status_code=404, heading="Hudu isn't set up", body=msg)


@app.exception_handler(auth.Forbidden)
async def _forbidden(request: Request, exc):
    return page(
        request, "message.html", status_code=403, heading="Admins only", body="Ask an admin to make this change."
    )


app.include_router(login.router)
app.include_router(tags.router)
app.include_router(printing.router)
app.include_router(settings_routes.router)
app.include_router(clients.router)
app.include_router(coverage.router)
app.include_router(users.router)
app.include_router(public.router)  # last: its /{raw} scan route matches any single path segment
