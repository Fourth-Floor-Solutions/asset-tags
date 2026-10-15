"""Public pages: favicon, version, health check, What's new, and the scan landing page."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from .. import auth, changelog, codes, db, labels, settings, version
from ..tagops import (
    label_name,
    mark_seen,
)
from ..web import PUBLIC_BASE_URL, go, page, tel_link

router = APIRouter()

# Public scan lookups per client IP, to slow down stepping through codes.
SCAN_WINDOW = 60
SCAN_MAX = 60
_scan_hits: defaultdict[str, deque] = defaultdict(deque)


@router.get("/favicon.ico")
def favicon_ico():
    return RedirectResponse("/favicon.svg", status_code=301)


@router.get("/favicon.svg")
def favicon_svg():
    """The organisation's short mark on its accent colour."""
    from html import escape

    from starlette.responses import Response

    org = settings.get_all()
    mark = escape((org["org_mark"] or "TD")[:3])
    size = 15 if len(mark) <= 2 else 11
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6" '
        f'fill="{escape(org["accent"])}"/><text x="16" y="22" text-anchor="middle" font-family="Menlo, monospace" '
        f'font-weight="800" font-size="{size}" fill="#111">{mark}</text></svg>'
    )
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=300"})


@router.get("/manifest.webmanifest")
def manifest():
    """Lets staff add TagDesk to a phone's home screen; it opens on Tag a site."""
    org = settings.get_all()
    return JSONResponse(
        {
            "name": f"TagDesk · {org['org_short']}",
            "short_name": "TagDesk",
            "start_url": "/tag-a-site",
            "scope": "/",
            "display": "standalone",
            "background_color": "#15171b",
            "theme_color": org["accent"],
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            ],
        },
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/icon-{size}.png")
def app_icon(size: int):
    """Home-screen icon: the organisation's mark on its accent colour."""
    import io
    import os

    from PIL import Image, ImageDraw, ImageFont

    if size not in (180, 192, 512):
        return PlainTextResponse("Not found", status_code=404)
    org = settings.get_all()
    accent = org["accent"] if len(org["accent"]) == 7 else "#5b8def"
    img = Image.new("RGB", (size, size), accent)
    mark = (org["org_mark"] or "TD")[:3]
    font = ImageFont.truetype(
        os.path.join(os.path.dirname(labels.__file__), "fonts", "JetBrainsMono-ExtraBold.ttf"),
        int(size * (0.42 if len(mark) <= 2 else 0.3)),
    )
    ImageDraw.Draw(img).text((size / 2, size / 2), mark, font=font, fill=settings.ink_for(accent), anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


@router.get("/changelog", response_class=HTMLResponse)
def whats_new(request: Request):
    """What's new: CHANGELOG.md, grouped by date."""
    try:
        days = changelog.load()
        error = None
    except (OSError, changelog.ChangelogError) as e:
        days, error = [], str(e)
    return page(
        request,
        "changelog.html",
        days=days,
        error=error,
        types=changelog.TYPES,
        inline=changelog.inline_html,
        day_label=changelog.label,
    )


@router.get("/version")
def version_info():
    return {"name": "TagDesk", "version": version.VERSION, "commit": version.COMMIT or None}


@router.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


@router.get("/{raw}", response_class=HTMLResponse)
def scan(request: Request, raw: str):
    code = codes.parse(raw)
    if not code:
        return page(
            request, "message.html", status_code=404, heading="Page not found", body="Check the address and try again."
        )
    user = getattr(request.state, "user", None)
    if not user:
        ip = request.client.host if request.client else "?"
        hits = _scan_hits[ip]
        now_t = time.monotonic()
        while hits and hits[0] < now_t - SCAN_WINDOW:
            hits.popleft()
        if len(hits) >= SCAN_MAX:
            return page(
                request,
                "message.html",
                status_code=429,
                heading="Too many lookups",
                body="Please wait a minute and try again.",
            )
        hits.append(now_t)
    with db.connection() as conn:
        tag = db.get_tag(conn, code)
        if user and tag:
            # Staff scanning a tag's QR link counts as seeing it (stocktake); not added to the history.
            mark_seen(conn, code, auth.label(user), log_it=False)
    if user:
        return go(f"/tags/{code}")
    org = settings.get_all()
    return page(
        request,
        "public.html",
        status_code=200 if tag else 404,
        tag=tag,
        code=code,
        support_phone=org["support_phone"],
        support_tel=tel_link(org["support_phone"]),
        support_email=org["support_email"],
        org=org,
        owner=(label_name(tag, org) if tag and tag["format"] != "loan" else None),
        report_href=report_mailto(tag, org) if tag and tag["status"] != "retired" else "",
    )


def report_mailto(tag, org: dict) -> str:
    """A mailto: link to the support desk with the device's details filled in (no server-side form)."""
    email = (org.get("support_email") or "").strip()
    if not email or "@" not in email:
        return ""
    client = org["org_short"] if tag["format"] == "loan" else (tag["client_name"] or "")
    device = tag["device_name"] or tag["model"] or "device"
    values = {"code": tag["code"], "device": device, "client": client}
    template = org.get("report_subject") or "Problem with {code}"
    subject = template
    for k, v in values.items():
        subject = subject.replace("{" + k + "}", v)
    body = (
        f"Tag: {tag['code']}\n"
        f"Device: {tag['device_name'] or '-'}\n"
        f"Client: {client or '-'}\n"
        f"Link: {PUBLIC_BASE_URL}/{tag['code']}\n\n"
        "What's wrong?\n\n"
    )
    return f"mailto:{quote(email, safe='@')}?subject={quote(subject[:200])}&body={quote(body)}"
