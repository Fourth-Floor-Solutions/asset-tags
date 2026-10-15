"""Rendering pages and small request helpers shared by every route module."""

from __future__ import annotations

import hashlib
import os
import random
import re
from datetime import date
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import auth, codes, db, hudu, labels, settings, usage, version

HERE = os.path.dirname(__file__)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")
SECRET_WARN_DAYS = 30

templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


def _static_versions() -> dict:
    """Content hash per static file, so each deploy changes the asset URLs.

    Reverse proxies that cache assets can send long Expires headers for js/css, which keep
    browsers on an old app.js after a deploy; a changed ?v= forces a refetch.
    """
    out = {}
    folder = os.path.join(HERE, "static")
    for name in os.listdir(folder):
        with open(os.path.join(folder, name), "rb") as f:
            out[name] = hashlib.sha256(f.read()).hexdigest()[:10]
    return out


_VERSIONS = _static_versions()
templates.env.globals["static"] = lambda name: f"/static/{name}?v={_VERSIONS.get(name, '0')}"


def tag_streams(rows: int = 16, per_row: int = 40) -> list:
    """Random-looking tag codes for the sign-in page's decorative background."""
    rnd = random.Random()
    return [[codes.to_code(rnd.randrange(codes.SPACE)) for _ in range(per_row)] for _ in range(rows)]


def page(request: Request, name: str, status_code: int = 200, **ctx):
    if name == "login.html":
        ctx.setdefault("streams", tag_streams())
    elif getattr(request.state, "user", None):
        # Signed-in pages get a faint, still version of the sign-in background.
        ctx.setdefault("bg_streams", tag_streams(rows=16, per_row=36))
    org = ctx.pop("org", None) or settings.get_all()
    ctx.update(
        request=request,
        org=org,
        accent_ink=settings.ink_for(org["accent"]),
        favicon=f"/favicon.svg?v={abs(hash((org['org_mark'], org['accent']))) % 10**8}",
        user_label=auth.label,
        user=getattr(request.state, "user", None),
        flash=request.session.pop("flash", None),
        sso=auth.sso_enabled(),
        hudu_on=hudu.configured(),
        printer_on=any_printer(),
        formats=labels.FORMATS,
        enabled_formats=labels.enabled(),
        tape_fill=labels.fill,
        public_base=PUBLIC_BASE_URL,
        # AGPL: users must be able to get the source; set your own link in Settings if you modify it.
        source_url=org["source_url"].rstrip("/"),
        secret_warning=secret_warning(ctx.get("user") or getattr(request.state, "user", None)),
        tape_warnings=tape_warnings(request),
        version=version.VERSION,
        commit=version.COMMIT,
    )
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def flash(request: Request, text: str, kind: str = "ok") -> None:
    request.session["flash"] = {"text": text, "kind": kind}


def go(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def hudu_link(url: str) -> str:
    """A Hudu asset link only if it's http(s) on the configured Hudu host; otherwise none.

    The value comes from the browser, so a javascript: or other-site link must never
    be stored and shown as 'Open in Hudu'.
    """
    url = clean(url, 500)
    base = urlparse(hudu.base_url())
    u = urlparse(url)
    if u.scheme in ("http", "https") and base.netloc and u.netloc.lower() == base.netloc.lower():
        return url
    return ""


def safe_next(target: str) -> str:
    """Where to go after signing in: only a path on this site, never another site.

    Rejects //host and /\\host (browsers treat both as another site), schemes,
    and control characters.
    """
    t = (target or "").strip()
    if not t.startswith("/") or t.startswith("//") or t.startswith("/\\") or "\\" in t or any(ord(ch) < 32 for ch in t):
        return "/"
    return t


def tel_link(phone: str) -> str:
    """'+44 (0)20 7946 0000' -> '+442079460000': the bracketed trunk 0 must not be dialled."""
    return re.sub(r"[^\d+]", "", re.sub(r"\(0\)", "", phone or ""))


def clean(v: str | None, limit: int = 200) -> str:
    return (v or "").strip()[:limit]


def secret_warning(user) -> str:
    """For admins: the Microsoft client secret expires soon (or has expired)."""
    if not user or user["role"] != "admin":
        return ""
    raw = settings.get("entra_secret_expires")
    try:
        expires = date.fromisoformat(raw)
    except (TypeError, ValueError):
        return ""
    days = (expires - date.today()).days
    if days < 0:
        return f"The Microsoft sign-in client secret expired on {expires:%d %B %Y}. Create a new one in Entra and enter it in Settings."
    if days <= SECRET_WARN_DAYS:
        return f"The Microsoft sign-in client secret expires in {days} day{'s' if days != 1 else ''} ({expires:%d %B %Y}). Create a new one in Entra and enter it in Settings."
    return ""


def tape_warnings(request: Request) -> list[dict]:
    """Low or empty cassettes, for the banner on staff pages (not the public scan page)."""
    if not getattr(request.state, "user", None):
        return []
    with db.connection() as conn:
        return usage.low_cassettes(conn)


def any_printer() -> bool:
    with db.connection() as conn:
        return conn.execute("SELECT 1 FROM printers LIMIT 1").fetchone() is not None
