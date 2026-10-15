"""Signing in and out: local accounts and Microsoft sign-in."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import auth, db, log
from ..web import PUBLIC_BASE_URL, go, page, safe_next

router = APIRouter()
_log = log.get("auth")


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return page(request, "login.html", next=safe_next(next))


# Failed sign-ins per client IP; the sign-in page is usually internet-facing.
LOGIN_WINDOW = 15 * 60
LOGIN_MAX_FAILS = 10
_login_fails: defaultdict[str, deque[float]] = defaultdict(deque)


def _login_blocked(ip: str) -> bool:
    q = _login_fails[ip]
    cutoff = time.monotonic() - LOGIN_WINDOW
    while q and q[0] < cutoff:
        q.popleft()
    return len(q) >= LOGIN_MAX_FAILS


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    ip = request.client.host if request.client else "?"
    if _login_blocked(ip):
        _log.warning("sign-in refused from %s: too many failed attempts", ip)
        return page(
            request,
            "login.html",
            status_code=429,
            next=next,
            error="Too many failed sign-ins. Wait 15 minutes and try again.",
        )
    with db.connection() as conn:
        # Accept either the username or the account's email address.
        who = username.strip()
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? OR (email != '' AND email = ?) ORDER BY username = ? DESC LIMIT 1",
            (who, who, who),
        ).fetchone()
    # Always run the password hash, so unknown usernames take as long as wrong passwords.
    if not auth.verify_password(password, row["password"] if row else auth.DUMMY_HASH) or not row:
        _login_fails[ip].append(time.monotonic())
        _log.warning("failed sign-in for %r from %s", username.strip()[:80], ip)
        return page(request, "login.html", status_code=401, next=next, error="Username or password is wrong.")
    _login_fails.pop(ip, None)
    _log.info("%s signed in from %s", row["username"], ip)
    request.session.clear()
    request.session["uid"] = row["id"]
    return go(safe_next(next))


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return go("/login")


@router.get("/auth/login")
async def sso_login(request: Request, next: str = "/"):
    client = auth.oauth()
    if not client:
        return go("/login")
    request.session["sso_next"] = safe_next(next)
    return await client.entra.authorize_redirect(request, f"{PUBLIC_BASE_URL}/auth/callback")


@router.get("/auth/callback")
async def sso_callback(request: Request):
    client = auth.oauth()
    if not client:
        return go("/login")
    try:
        token = await client.entra.authorize_access_token(request)
    except Exception as e:
        _log.warning("Microsoft sign-in failed: %s", e)
        return page(
            request, "login.html", status_code=401, next="/", error="Microsoft sign-in didn't complete. Try again."
        )
    info = token.get("userinfo") or {}
    cfg = auth.sso_config()
    if cfg and auth.GUID_RE.match(cfg[0]) and (info.get("tid") or "").lower() != cfg[0].lower():
        _log.warning("Microsoft sign-in refused: account from another tenant (%s)", info.get("tid"))
        return page(
            request,
            "login.html",
            status_code=403,
            next="/",
            error="That Microsoft account belongs to a different organisation.",
        )
    email = info.get("email") or info.get("preferred_username") or ""
    row = auth.sso_user_for(email, info.get("name") or "")
    if not row:
        _log.warning("Microsoft sign-in refused: %s has no TagDesk account", email or "unknown account")
        return page(
            request,
            "login.html",
            status_code=403,
            next="/",
            error=f"{email or 'That account'} doesn't have access. Ask an admin to add you.",
        )
    _log.info("%s signed in with Microsoft", row["username"])
    nxt = safe_next(request.session.pop("sso_next", "/"))
    request.session.clear()
    request.session["uid"] = row["id"]
    return go(nxt)
