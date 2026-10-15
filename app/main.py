"""TagDesk: issue, print and look up client-branded asset tags."""
from __future__ import annotations

import os
import secrets
import time
from collections import defaultdict, deque
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, codes, db, hudu, labels, printer, settings, usage

HERE = os.path.dirname(__file__)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")
# AGPL: people using the app over a network must be able to get its source.
# If you run a modified version, point this at your own copy of the code.
SOURCE_URL = os.environ.get("SOURCE_URL", "https://github.com/Fourth-Floor-Solutions/tagdesk")
MAX_BATCH = 100
MAX_PRINT = 200


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


db.init()
auth.seed_admin()
labels.apply_overrides(settings.formats())

app = FastAPI(title="TagDesk", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


def _static_versions() -> dict:
    """Content hash per static file, so each deploy changes the asset URLs.

    Reverse proxies that cache assets can send long Expires headers for js/css, which keep
    browsers on an old app.js after a deploy; a changed ?v= forces a refetch.
    """
    import hashlib

    out = {}
    folder = os.path.join(HERE, "static")
    for name in os.listdir(folder):
        with open(os.path.join(folder, name), "rb") as f:
            out[name] = hashlib.sha256(f.read()).hexdigest()[:10]
    return out


_VERSIONS = _static_versions()
templates.env.globals["static"] = lambda name: f"/static/{name}?v={_VERSIONS.get(name, '0')}"


SETUP_EXEMPT = ("/setup", "/static/", "/favicon", "/login", "/logout", "/auth/", "/healthz")


@app.middleware("http")
async def load_user(request: Request, call_next):
    request.state.user = auth.session_user(request)
    user = request.state.user
    # A new install sends its first admin to the setup screen until it's saved.
    if (user and user["role"] == "admin" and request.method == "GET"
            and not request.url.path.startswith(SETUP_EXEMPT) and not settings.setup_done()):
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


@app.exception_handler(auth.Forbidden)
async def _forbidden(request: Request, exc):
    return page(request, "message.html", status_code=403,
                heading="Admins only", body="Ask an admin to make this change.")


# ---------- helpers ----------

def page(request: Request, name: str, status_code: int = 200, **ctx):
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
        source_url=SOURCE_URL,
    )
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def flash(request: Request, text: str, kind: str = "ok") -> None:
    request.session["flash"] = {"text": text, "kind": kind}


def go(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def label_name(tag, org: dict = None) -> str:
    org = org or settings.get_all()
    return org["org_short"] if tag["format"] == "loan" else tag["client_label"]


def label_text(org: dict = None) -> dict:
    return settings.label_text(org or settings.get_all())


def printed_as(tag) -> str:
    return f"{tag['format']}|{label_name(tag)}"


def needs_reprint(tag) -> bool:
    return bool(tag["printed_as"]) and tag["printed_as"] != printed_as(tag)


def svg_for(tag, org: dict = None) -> str:
    org = org or settings.get_all()
    return labels.render(tag["format"], tag["code"], label_name(tag, org), PUBLIC_BASE_URL, text=label_text(org))


HUDU_OK = ("written", "cleared", "already up to date")


def hudu_needs_sync(tag) -> bool:
    desired = tag["hudu_asset_id"] if tag["status"] != "retired" else None
    status = tag["hudu_sync_status"] or ""
    return desired != tag["hudu_pushed_asset_id"] or (desired is not None and not status.startswith(HUDU_OK))


def hudu_sync(conn, code: str, actor: str, force: bool = False) -> Optional[str]:
    """Keep the linked Hudu asset's Asset Tag field in step with this tag.

    Writes the code to the linked asset, clears it from a previously linked
    asset (only if that asset still holds this code), and clears it when the
    tag is retired. Returns an error message, or None when all went well.
    """
    if not hudu.write_enabled():
        return None
    tag = db.get_tag(conn, code)
    if not tag or not (force or hudu_needs_sync(tag)):
        return None
    desired = tag["hudu_asset_id"] if tag["status"] != "retired" else None
    pushed = tag["hudu_pushed_asset_id"]
    notes, error = [], None
    try:
        if pushed and pushed != desired:
            notes.append(f"asset {pushed}: {hudu.set_tag(pushed, '', only_if=code)}")
            pushed = None
        if desired:
            result = hudu.set_tag(desired, code)
            notes.append(f"asset {desired}: {result}")
            pushed = desired if result in HUDU_OK else None
    except hudu.HuduError as e:
        error = str(e)
        notes.append(f"error: {error}")
    status = "; ".join(notes) or "nothing to do"
    if error:
        status = "error: " + error
    elif desired:
        status = notes[-1].split(": ", 1)[1]
    conn.execute("UPDATE tags SET hudu_pushed_asset_id=?, hudu_sync_status=?, hudu_sync_at=? WHERE code=?",
                 (pushed, status, db.now(), code))
    db.log(conn, code, actor, "hudu", "; ".join(notes))
    return error


def clients(conn, include_inactive: bool = False):
    sql = "SELECT * FROM clients" + ("" if include_inactive else " WHERE active = 1")
    return conn.execute(sql + " ORDER BY is_internal DESC, name COLLATE NOCASE").fetchall()


def tel_link(phone: str) -> str:
    """'+44 (0)20 7946 0000' -> '+442079460000': the bracketed trunk 0 must not be dialled."""
    import re

    return re.sub(r"[^\d+]", "", re.sub(r"\(0\)", "", phone or ""))


def clean(v: Optional[str], limit: int = 200) -> str:
    return (v or "").strip()[:limit]


def check_format(conn, client_id: int, fmt: str, current_format: str = None) -> Optional[str]:
    if fmt not in labels.FORMATS:
        return "Choose a label format."
    if not labels.FORMATS[fmt].get("enabled", True) and fmt != current_format:
        return f"{labels.FORMATS[fmt]['name']} tags are switched off in Settings."
    c = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not c:
        return "Choose a client."
    if fmt == "loan" and not c["is_internal"]:
        own = conn.execute("SELECT name FROM clients WHERE is_internal = 1 LIMIT 1").fetchone()
        return ("Loan tags are only for your own kit. "
                + (f"Choose {own['name']} as the client." if own else "Set your organisation's client in Settings first."))
    return None


# ---------- health & auth ----------

@app.get("/favicon.ico")
def favicon_ico():
    return RedirectResponse("/favicon.svg", status_code=301)


@app.get("/favicon.svg")
def favicon_svg():
    """The organisation's short mark on its accent colour."""
    from html import escape
    from starlette.responses import Response

    org = settings.get_all()
    mark = escape((org["org_mark"] or "TD")[:3])
    size = 15 if len(mark) <= 2 else 11
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6" '
           f'fill="{escape(org["accent"])}"/><text x="16" y="22" text-anchor="middle" font-family="Menlo, monospace" '
           f'font-weight="800" font-size="{size}" fill="#111">{mark}</text></svg>')
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=300"})


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return page(request, "login.html", next=next if next.startswith("/") else "/")


# Failed sign-ins per client IP; the sign-in page is usually internet-facing.
LOGIN_WINDOW = 15 * 60
LOGIN_MAX_FAILS = 10
_login_fails = defaultdict(deque)


def _login_blocked(ip: str) -> bool:
    q = _login_fails[ip]
    cutoff = time.monotonic() - LOGIN_WINDOW
    while q and q[0] < cutoff:
        q.popleft()
    return len(q) >= LOGIN_MAX_FAILS


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    ip = request.client.host if request.client else "?"
    if _login_blocked(ip):
        return page(request, "login.html", status_code=429, next=next,
                    error="Too many failed sign-ins. Wait 15 minutes and try again.")
    conn = db.connect()
    try:
        # Accept either the username or the account's email address.
        who = username.strip()
        row = conn.execute("SELECT * FROM users WHERE username = ? OR (email != '' AND email = ?) "
                           "ORDER BY username = ? DESC LIMIT 1", (who, who, who)).fetchone()
    finally:
        conn.close()
    if not row or not auth.verify_password(password, row["password"]):
        _login_fails[ip].append(time.monotonic())
        return page(request, "login.html", status_code=401, next=next, error="Username or password is wrong.")
    _login_fails.pop(ip, None)
    request.session.clear()
    request.session["uid"] = row["id"]
    return go(next if next.startswith("/") and not next.startswith("//") else "/")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return go("/login")


@app.get("/auth/login")
async def sso_login(request: Request, next: str = "/"):
    client = auth.oauth()
    if not client:
        return go("/login")
    request.session["sso_next"] = next if next.startswith("/") else "/"
    return await client.entra.authorize_redirect(request, f"{PUBLIC_BASE_URL}/auth/callback")


@app.get("/auth/callback")
async def sso_callback(request: Request):
    client = auth.oauth()
    if not client:
        return go("/login")
    try:
        token = await client.entra.authorize_access_token(request)
    except Exception:
        return page(request, "login.html", status_code=401, next="/", error="Microsoft sign-in didn't complete. Try again.")
    info = token.get("userinfo") or {}
    email = info.get("email") or info.get("preferred_username") or ""
    row = auth.sso_user_for(email, info.get("name") or "")
    if not row:
        return page(request, "login.html", status_code=403, next="/",
                    error=f"{email or 'That account'} doesn't have access. Ask an admin to add you.")
    nxt = request.session.pop("sso_next", "/")
    request.session.clear()
    request.session["uid"] = row["id"]
    return go(nxt)


# ---------- dashboard ----------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, q: str = "", client_id: str = "", status: str = ""):
    auth.require_user(request)
    q = q.strip()
    code = codes.parse(q) if q else None
    if code:
        conn = db.connect()
        try:
            if db.get_tag(conn, code):
                return go(f"/tags/{code}")
        finally:
            conn.close()
    where, args = [], []
    if q:
        like = f"%{q}%"
        where.append("(t.code LIKE ? OR t.serial LIKE ? OR t.device_name LIKE ? OR t.model LIKE ? "
                     "OR c.name LIKE ? OR c.label_name LIKE ? OR t.notes LIKE ?)")
        args += [f"%{codes.normalise(q)}%", like, like, like, like, like, like]
    if client_id.isdigit():
        where.append("t.client_id = ?")
        args.append(int(client_id))
    if status in ("unassigned", "active", "retired"):
        where.append("t.status = ?")
        args.append(status)
    sql = db.TAG_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY t.updated_at DESC LIMIT 300"
    conn = db.connect()
    try:
        rows = conn.execute(sql, args).fetchall()
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM tags GROUP BY status")}
        reprints = [r for r in conn.execute(db.TAG_SELECT + " WHERE t.printed_as IS NOT NULL AND t.status != 'retired'")
                    if needs_reprint(r)]
        cl = clients(conn)
    finally:
        conn.close()
    return page(request, "dashboard.html", tags=rows, q=q, client_id=client_id, status=status,
                counts=counts, reprint_count=len(reprints), clients=cl, needs_reprint=needs_reprint)


# ---------- issuing ----------

@app.get("/tags/new", response_class=HTMLResponse)
def tag_new_form(request: Request, client_id: str = ""):
    auth.require_user(request)
    conn = db.connect()
    try:
        cl = clients(conn)
    finally:
        conn.close()
    return page(request, "tag_new.html", clients=cl, sel_client=client_id, form={})


@app.post("/tags/new")
def tag_new(request: Request, client_id: int = Form(...), format: str = Form("standard"),
            device_name: str = Form(""), model: str = Form(""), serial: str = Form(""), notes: str = Form(""),
            hudu_asset_id: str = Form(""), hudu_asset_url: str = Form("")):
    user = auth.require_user(request)
    fields = {
        "device_name": clean(device_name, 120), "model": clean(model, 120), "serial": clean(serial, 120),
        "notes": clean(notes, 2000),
        "hudu_asset_id": int(hudu_asset_id) if hudu_asset_id.isdigit() else None,
        "hudu_asset_url": clean(hudu_asset_url, 500) if hudu_asset_id.isdigit() else "",
    }
    conn = db.connect()
    try:
        err = check_format(conn, client_id, format)
        if not err and fields["serial"]:
            dup = conn.execute("SELECT code FROM tags WHERE serial = ? AND status != 'retired'",
                               (fields["serial"],)).fetchone()
            if dup:
                err = f"Serial {fields['serial']} is already tagged as {dup['code']}. Open that tag instead."
        if err:
            return page(request, "tag_new.html", status_code=400, clients=clients(conn),
                        sel_client=str(client_id), form=dict(fields, format=format), error=err)
        code = db.issue_tags(conn, 1, client_id, format, auth.label(user), fields)[0]
        herr = hudu_sync(conn, code, auth.label(user))
    finally:
        conn.close()
    if herr:
        flash(request, f"Tag {code} issued, but it couldn't be written to Hudu: {herr}", "warn")
    else:
        flash(request, f"Tag {code} issued. Print it and stick it on the device.")
    return go(f"/tags/{code}")


@app.get("/tags/batch", response_class=HTMLResponse)
def batch_form(request: Request):
    auth.require_user(request)
    conn = db.connect()
    try:
        cl = clients(conn)
    finally:
        conn.close()
    return page(request, "batch.html", clients=cl)


@app.post("/tags/batch")
def batch(request: Request, client_id: int = Form(...), format: str = Form("standard"), count: int = Form(...)):
    user = auth.require_user(request)
    conn = db.connect()
    try:
        err = check_format(conn, client_id, format)
        if not err and not 1 <= count <= MAX_BATCH:
            err = f"Issue between 1 and {MAX_BATCH} tags at a time."
        if err:
            return page(request, "batch.html", status_code=400, clients=clients(conn), error=err)
        issued = db.issue_tags(conn, count, client_id, format, auth.label(user))
    finally:
        conn.close()
    return go("/print?codes=" + ",".join(issued) + "&batch=1")


# ---------- one tag ----------

@app.get("/tags/{code}", response_class=HTMLResponse)
def tag_detail(request: Request, code: str):
    auth.require_user(request)
    canon = codes.parse(code)
    if not canon:
        return page(request, "message.html", status_code=404, heading="Not a tag code",
                    body="Tag codes are five characters, like 7KQ3M.")
    if canon != code:
        return go(f"/tags/{canon}")
    conn = db.connect()
    try:
        tag = db.get_tag(conn, canon)
        if not tag:
            return page(request, "message.html", status_code=404, heading=f"{canon} hasn't been issued",
                        body="No tag with this code exists. Check the code on the label, or issue a new tag.")
        events = conn.execute("SELECT * FROM events WHERE code = ? ORDER BY id DESC", (canon,)).fetchall()
        cl = clients(conn, include_inactive=True)
    finally:
        conn.close()
    return page(request, "tag.html", tag=tag, svg=svg_for(tag), events=events, clients=cl,
                hudu_write=hudu.write_enabled(), hudu_field=hudu.tag_label(),
                reprint=needs_reprint(tag), qr_url=labels.qr_data(PUBLIC_BASE_URL, canon))


@app.post("/tags/{code}")
def tag_update(request: Request, code: str, client_id: int = Form(...), format: str = Form(...),
               device_name: str = Form(""), model: str = Form(""), serial: str = Form(""), notes: str = Form(""),
               hudu_asset_id: str = Form(""), hudu_asset_url: str = Form("")):
    user = auth.require_user(request)
    conn = db.connect()
    try:
        tag = db.get_tag(conn, code)
        if not tag:
            return go("/")
        err = check_format(conn, client_id, format, current_format=tag["format"])
        if err:
            flash(request, err, "bad")
            return go(f"/tags/{code}")
        new = {
            "client_id": client_id, "format": format,
            "device_name": clean(device_name, 120), "model": clean(model, 120), "serial": clean(serial, 120),
            "notes": clean(notes, 2000),
            "hudu_asset_id": int(hudu_asset_id) if hudu_asset_id.isdigit() else None,
            "hudu_asset_url": clean(hudu_asset_url, 500) if hudu_asset_id.isdigit() else "",
        }
        changes = []
        for k, v in new.items():
            if (tag[k] or None) != (v or None):
                if k == "client_id":
                    old_c = tag["client_name"]
                    new_c = conn.execute("SELECT name FROM clients WHERE id = ?", (v,)).fetchone()["name"]
                    changes.append(f"client: {old_c} → {new_c}")
                elif k in ("notes", "hudu_asset_url"):
                    changes.append(k.replace("_", " "))
                else:
                    changes.append(f"{k.replace('_', ' ')}: {tag[k] or '—'} → {v or '—'}")
        if not changes:
            return go(f"/tags/{code}")
        status = tag["status"]
        if status == "unassigned" and (new["serial"] or new["device_name"] or new["hudu_asset_id"]):
            status = "active"
        conn.execute(
            "UPDATE tags SET client_id=?, format=?, device_name=?, model=?, serial=?, notes=?, hudu_asset_id=?, "
            "hudu_asset_url=?, status=?, updated_at=? WHERE code=?",
            (new["client_id"], new["format"], new["device_name"], new["model"], new["serial"], new["notes"],
             new["hudu_asset_id"], new["hudu_asset_url"], status, db.now(), code),
        )
        db.log(conn, code, auth.label(user), "edited", "; ".join(changes))
        herr = hudu_sync(conn, code, auth.label(user))
        updated = db.get_tag(conn, code)
    finally:
        conn.close()
    if herr:
        flash(request, f"Saved, but Hudu wasn't updated: {herr}", "warn")
    elif needs_reprint(updated):
        flash(request, "Saved. The printed label no longer matches, so print a replacement.", "warn")
    else:
        flash(request, "Saved.")
    return go(f"/tags/{code}")


@app.post("/tags/{code}/status")
def tag_status(request: Request, code: str, status: str = Form(...), reason: str = Form("")):
    user = auth.require_user(request)
    if status not in ("retired", "active", "unassigned"):
        return go(f"/tags/{code}")
    conn = db.connect()
    try:
        tag = db.get_tag(conn, code)
        if tag and tag["status"] != status:
            conn.execute("UPDATE tags SET status=?, updated_at=? WHERE code=?", (status, db.now(), code))
            action = "retired" if status == "retired" else "restored"
            db.log(conn, code, auth.label(user), action, clean(reason, 300))
            herr = hudu_sync(conn, code, auth.label(user))
            if herr:
                flash(request, f"Tag {action}, but Hudu wasn't updated: {herr}", "warn")
    finally:
        conn.close()
    return go(f"/tags/{code}")


@app.post("/tags/{code}/hudu")
def tag_hudu(request: Request, code: str):
    user = auth.require_user(request)
    conn = db.connect()
    try:
        err = hudu_sync(conn, code, auth.label(user), force=True)
        tag = db.get_tag(conn, code)
    finally:
        conn.close()
    if err:
        flash(request, f"Hudu wasn't updated: {err}", "bad")
    elif tag:
        flash(request, f"Hudu: {tag['hudu_sync_status']}.")
    return go(f"/tags/{code}")


@app.post("/hudu/sync-all")
def hudu_sync_all(request: Request):
    user = auth.require_admin(request)
    if not hudu.write_enabled():
        flash(request, "Writing to Hudu is switched off (HUDU_WRITE isn't 1).", "bad")
        return go("/clients")
    conn = db.connect()
    done = failed = 0
    try:
        for t in conn.execute(db.TAG_SELECT).fetchall():
            if not hudu_needs_sync(t):
                continue
            if hudu_sync(conn, t["code"], auth.label(user)):
                failed += 1
            else:
                done += 1
    finally:
        conn.close()
    flash(request, f"Hudu updated for {done} tag(s)" + (f"; {failed} failed, open them to see why." if failed else "."),
          "warn" if failed else "ok")
    return go("/clients")


# ---------- printing ----------

def _print_codes(raw: str) -> list:
    out = []
    for part in raw.split(","):
        c = codes.parse(part)
        if c and c not in out:
            out.append(c)
    return out[:MAX_PRINT]


@app.get("/print", response_class=HTMLResponse)
def print_sheet(request: Request, batch: str = ""):
    auth.require_user(request)
    wanted = _print_codes(request.query_params.get("codes", ""))
    conn = db.connect()
    try:
        tags = [t for t in (db.get_tag(conn, c) for c in wanted) if t]
    finally:
        conn.close()
    org = settings.get_all()
    items = [{"tag": t, "svg": svg_for(t, org), "spec": labels.FORMATS[t["format"]]} for t in tags]
    used = sorted({t["format"] for t in tags})
    pstats = printers_live()
    plan, waiting = printer.route(tags, pstats)
    names = {x["p"]["id"]: x["p"]["name"] for x in pstats}
    target = {t["code"]: names[pid] for pid, ts in plan.items() for t in ts}
    return page(request, "print.html", items=items, used_formats=used, batch=batch == "1",
                pstats=pstats, target=target, n_ready=len(target),
                code_list=",".join(t["code"] for t in tags))


def record_printed(conn, tags: list, actor: str, how: str) -> int:
    for tag in tags:
        conn.execute(
            "UPDATE tags SET printed_count = printed_count + 1, printed_at = ?, printed_as = ? WHERE code = ?",
            (db.now(), printed_as(tag), tag["code"]),
        )
        db.log(conn, tag["code"], actor, "printed", f"{labels.FORMATS[tag['format']]['name']} · {how}")
    return len(tags)


@app.post("/print/record")
def print_record(request: Request, codes_csv: str = Form(..., alias="codes")):
    user = auth.require_user(request)
    conn = db.connect()
    try:
        tags = [t for t in (db.get_tag(conn, c) for c in _print_codes(codes_csv)) if t]
        n = record_printed(conn, tags, auth.label(user), "browser")
    finally:
        conn.close()
    return JSONResponse({"recorded": n})


NUDGE_LIMIT = 40   # dots (about 2.8 mm) either way


def printers_live() -> list:
    conn = db.connect()
    try:
        rows = printer.all_printers(conn)
        live = printer.with_status(rows)
        for x in live:
            usage.track(conn, x["p"], x["st"])
        return live
    finally:
        conn.close()


def any_printer() -> bool:
    conn = db.connect()
    try:
        return conn.execute("SELECT 1 FROM printers LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def _plural(n: int, word: str = "label") -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


@app.post("/print/send")
def print_send(request: Request, codes_csv: str = Form(..., alias="codes"), strip: str = Form("0")):
    """Print straight to the label printers, routing each label to one whose tape fits."""
    user = auth.require_user(request)
    pstats = printers_live()
    if not pstats:
        return JSONResponse({"ok": False, "message": "No label printer is set up. An admin can add one on the Printer page."},
                            status_code=502)
    conn = db.connect()
    printed, notes, problems = [], [], []
    try:
        tags = [t for t in (db.get_tag(conn, c) for c in _print_codes(codes_csv)) if t]
        if not tags:
            return JSONResponse({"ok": False, "message": "Nothing to print."})
        plan, waiting = printer.route(tags, pstats)
        for x in pstats:
            batch = plan.get(x["p"]["id"])
            if not batch:
                continue
            p, st = x["p"], x["st"]
            org = settings.get_all()
            images = [labels.raster(t["format"], t["code"], label_name(t, org), PUBLIC_BASE_URL, text=label_text(org))
                      for t in batch]
            try:
                printer.send(p["host"], printer.build_job(images, st["tape_mm"], p["nudge_dots"], strip=strip == "1"))
            except printer.PrinterError as e:
                problems.append(f"{p['name']}: {e}")
                waiting += batch
                continue
            problem = printer.wait_done(p["host"])
            if problem:
                problems.append(f"{p['name']}: {problem}")
            record_printed(conn, batch, auth.label(user), p["name"])
            usage.track(conn, p, st)
            left = usage.summary(conn, p["id"], st)
            if left and left["low"]:
                problems.append(f"Tape is low in {p['name']}: about {left['left_cm'] / 100:.1f} m left.")
            printed += batch
            notes.append(f"{_plural(len(batch))} on {p['name']} ({st['tape_mm']} mm {st['tape_colour']})"
                         + (" as one strip with cut lines" if strip == "1" and len(batch) > 1 else ""))
    finally:
        conn.close()
    if printed:
        msg = "Printed " + "; ".join(notes) + "."
    else:
        offline = [f"{x['p']['name']}: {x['err']}" for x in pstats if x["err"]]
        msg = "Nothing printed. " + (" ".join(offline) if offline and len(offline) == len(pstats) else "")
    if waiting:
        need = sorted({labels.FORMATS[t["format"]]["tape"] for t in waiting})
        msg += f" {_plural(len(waiting))} still to print: load {', '.join(need)} in a printer and press Print again."
    if problems:
        msg += " " + " ".join(problems)
    return JSONResponse({"ok": bool(printed) and not problems, "message": msg.strip(),
                         "printed": [t["code"] for t in printed], "waiting": [t["code"] for t in waiting]})


@app.get("/printer", response_class=HTMLResponse)
def printer_page(request: Request):
    auth.require_user(request)
    pstats = printers_live()
    conn = db.connect()
    try:
        for x in pstats:
            x["cassette"] = usage.summary(conn, x["p"]["id"], x["st"])
    finally:
        conn.close()
    for x in pstats:
        x["fits"] = [k for k in labels.enabled() if x["st"] and not printer.check_tape(x["st"], k)]
        x["nudge_mm"] = round(abs(x["p"]["nudge_dots"]) / labels.DOTS_PER_MM, 2)
    return page(request, "printer.html", pstats=pstats)


def _printer_form(name: str, address: str):
    host = printer.clean_host(address)
    name = clean(name, 60)
    if not host:
        return None, None, "Enter the printer's IP address or host name, for example 192.168.1.50."
    if not name:
        return None, None, "Give the printer a name, for example \"Office label printer\"."
    return name, host, None


def _check_message(name: str, host: str):
    try:
        st = printer.status(host)
        return f"{name} saved. Found a {st['model']} at {host} with {st['tape_mm'] or 'no'} mm tape loaded.", "ok"
    except printer.PrinterError as e:
        return f"{name} saved, but nothing answered at {host} yet ({e}).", "warn"


@app.post("/cassettes/{cid}/new")
def cassette_new(request: Request, cid: int):
    auth.require_user(request)
    conn = db.connect()
    try:
        conn.execute("UPDATE cassettes SET used_cm = 0, estimated = 0, fitted_at = ? WHERE id = ?", (db.now(), cid))
    finally:
        conn.close()
    flash(request, "New cassette recorded. Tape use starts again from zero.")
    return go("/printer")


@app.post("/cassettes/{cid}")
def cassette_set(request: Request, cid: int, used_m: str = Form(""), length_m: str = Form("")):
    auth.require_user(request)
    try:
        used = round(float(used_m or 0) * 100)
        length = round(float(length_m or 8) * 100)
    except ValueError:
        flash(request, "Enter metres as a number, for example 2.5.", "bad")
        return go("/printer")
    if not (0 <= used <= 10000 and 100 <= length <= 10000):
        flash(request, "That doesn't look right: used must be 0–100 m and the cassette length 1–100 m.", "bad")
        return go("/printer")
    conn = db.connect()
    try:
        conn.execute("UPDATE cassettes SET used_cm = ?, length_cm = ? WHERE id = ?", (used, length, cid))
    finally:
        conn.close()
    flash(request, "Cassette updated.")
    return go("/printer")


@app.post("/printers/new")
def printer_new(request: Request, name: str = Form(""), address: str = Form("")):
    auth.require_admin(request)
    name, host, err = _printer_form(name, address)
    if err:
        flash(request, err, "bad")
        return go("/printer")
    conn = db.connect()
    try:
        first = conn.execute("SELECT COUNT(*) FROM printers").fetchone()[0] == 0
        conn.execute("INSERT INTO printers(name, host, nudge_dots, is_default, created_at) VALUES(?,?,0,?,?)",
                     (name, host, 1 if first else 0, db.now()))
    finally:
        conn.close()
    flash(request, *_check_message(name, host))
    return go("/printer")


def _get_printer(conn, pid: int):
    return conn.execute("SELECT * FROM printers WHERE id = ?", (pid,)).fetchone()


@app.post("/printers/{pid}")
def printer_update(request: Request, pid: int, name: str = Form(""), address: str = Form(""),
                   dots: int = Form(0), then: str = Form("")):
    auth.require_admin(request)
    name, host, err = _printer_form(name, address)
    if err:
        flash(request, err, "bad")
        return go("/printer")
    dots = max(-NUDGE_LIMIT, min(NUDGE_LIMIT, dots))
    conn = db.connect()
    try:
        old = _get_printer(conn, pid)
        if not old:
            return go("/printer")
        conn.execute("UPDATE printers SET name=?, host=?, nudge_dots=? WHERE id=?", (name, host, dots, pid))
    finally:
        conn.close()
    if then == "test":
        return printer_test(request, pid)
    if host != old["host"]:
        flash(request, *_check_message(name, host))
    else:
        flash(request, f"{name} saved.")
    return go("/printer")


@app.post("/printers/{pid}/default")
def printer_default(request: Request, pid: int):
    auth.require_admin(request)
    conn = db.connect()
    try:
        if _get_printer(conn, pid):
            conn.execute("UPDATE printers SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END", (pid,))
            flash(request, "Default printer changed. It's tried first for every print.")
    finally:
        conn.close()
    return go("/printer")


@app.post("/printers/{pid}/delete")
def printer_delete(request: Request, pid: int):
    auth.require_admin(request)
    conn = db.connect()
    try:
        p = _get_printer(conn, pid)
        if p:
            conn.execute("DELETE FROM printers WHERE id = ?", (pid,))
            if p["is_default"]:
                conn.execute("UPDATE printers SET is_default = 1 WHERE id = (SELECT MIN(id) FROM printers)")
            flash(request, f"Removed {p['name']}.")
    finally:
        conn.close()
    return go("/printer")


@app.post("/printers/{pid}/test")
def printer_test(request: Request, pid: int):
    auth.require_user(request)
    conn = db.connect()
    try:
        p = _get_printer(conn, pid)
    finally:
        conn.close()
    if not p:
        return go("/printer")
    try:
        st = printer.status(p["host"])
    except printer.PrinterError as e:
        flash(request, str(e), "bad")
        return go("/printer")
    fmt = next((k for k in ("slim", "standard", "micro", "loan")
                if labels.FORMATS[k].get("enabled", True) and not printer.check_tape(st, k)), None)
    if not fmt:
        flash(request, f"None of the tag formats fit the tape in {p['name']} ({st['tape_mm']} mm {st['tape_colour']}).", "bad")
        return go("/printer")
    image = labels.raster(fmt, "TEST0", "Test print", PUBLIC_BASE_URL, text=label_text())
    try:
        printer.send(p["host"], printer.build_job([image], st["tape_mm"], p["nudge_dots"]))
    except printer.PrinterError as e:
        flash(request, str(e), "bad")
        return go("/printer")
    problem = printer.wait_done(p["host"])
    flash(request, problem or f"Test label sent to {p['name']} ({labels.FORMATS[fmt]['name']} format).",
          "bad" if problem else "ok")
    return go("/printer")


# ---------- settings ----------

def _settings_page(request: Request, setup: bool, error: str = "", form: dict = None):
    conn = db.connect()
    try:
        cl = clients(conn)
        internal = conn.execute("SELECT id FROM clients WHERE is_internal = 1 LIMIT 1").fetchone()
    finally:
        conn.close()
    s = form or settings.get_all()
    if not s.get("internal_client_id") and internal:
        s = dict(s, internal_client_id=str(internal["id"]))
    return page(request, "settings.html", status_code=400 if error else 200, setup=setup, s=s, error=error,
                tape_colours=list(labels.TAPE_FILLS),
                clients=cl, preview=label_text(s) if not error else None,
                env={"public_url": PUBLIC_BASE_URL, "hudu_key": bool(os.environ.get("HUDU_API_KEY")),
                     "sso": auth.sso_enabled(), "printer_count": len(printers_live_rows())})


def printers_live_rows() -> list:
    conn = db.connect()
    try:
        return printer.all_printers(conn)
    finally:
        conn.close()


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    auth.require_admin(request)
    if settings.setup_done():
        return go("/settings")
    return _settings_page(request, setup=True)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    auth.require_admin(request)
    return _settings_page(request, setup=False)


@app.post("/settings")
async def settings_save(request: Request):
    auth.require_admin(request)
    data = await request.form()
    setup = data.get("setup") == "1"
    v = {k: clean(data.get(k, ""), 200) for k in settings.KEYS}
    v["hudu_write"] = "1" if data.get("hudu_write") else "0"
    v["hudu_url"] = v["hudu_url"].rstrip("/")
    v["org_mark"] = v["org_mark"][:3]
    v["accent"] = v["accent"] or settings.DEFAULTS["accent"]
    for k in ("label_managed", "label_loan_top", "label_loan_bottom", "hudu_tag_field"):
        v[k] = v[k] or settings.DEFAULTS[k]
    error = ""
    if not v["org_name"]:
        error = "Enter your organisation's name."
    elif not v["org_mark"]:
        error = "Enter a short mark of 1 to 3 characters, for example your initials."
    elif not settings.HEX.match(v["accent"]):
        error = "The accent colour must be a hex colour like #5b8def."
    elif v["hudu_url"] and not v["hudu_url"].startswith(("https://", "http://")):
        error = "The Hudu address must start with https://"
    v["org_short"] = v["org_short"] or v["org_name"]
    # Format settings only change when the form included the format table.
    formats_shown = data.get("formats_shown") == "1"
    fmt_over = {}
    for key, base in (labels.BUILT_IN.items() if formats_shown else []):
        colour = data.get(f"fmt_{key}_colour", base["tape_colour"])
        fmt_over[key] = {
            "enabled": bool(data.get(f"fmt_{key}_enabled")),
            "tape_colour": colour if colour in labels.TAPE_FILLS else base["tape_colour"],
            "tape": clean(data.get(f"fmt_{key}_tape", ""), 80) or base["tape"],
        }
    if not error and formats_shown and not any(o["enabled"] for k, o in fmt_over.items() if k != "loan"):
        error = "Keep at least one client tag format (Standard, Slim or Micro) switched on."
    if error:
        return _settings_page(request, setup, error=error, form=v)
    conn = db.connect()
    try:
        # Your organisation's own client (used for loan tags). Create one if asked or if none exists.
        cid = v["internal_client_id"]
        if cid == "new" or (not cid and not conn.execute("SELECT 1 FROM clients WHERE is_internal = 1").fetchone()):
            conn.execute("INSERT INTO clients(hudu_id, name, label_name, is_internal, active, updated_at) "
                         "VALUES(NULL,?,?,1,1,?)", (v["org_name"], v["org_short"][:40], db.now()))
            cid = str(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        if cid.isdigit():
            conn.execute("UPDATE clients SET is_internal = CASE WHEN id = ? THEN 1 ELSE 0 END", (int(cid),))
        v["internal_client_id"] = cid if cid.isdigit() else ""
    finally:
        conn.close()
    settings.save(v)
    if formats_shown:
        settings.save_formats(fmt_over)
        labels.apply_overrides(fmt_over)
    if setup:
        settings.mark_setup_done()
        flash(request, "Setup saved. Next, add your label printer, then sync or add your clients.")
        return go("/printer")
    flash(request, "Settings saved.")
    return go("/settings")


# ---------- clients ----------

@app.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request):
    auth.require_user(request)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM tags t WHERE t.client_id = c.id AND t.status != 'retired') AS n "
            "FROM clients c ORDER BY c.active DESC, c.is_internal DESC, c.name COLLATE NOCASE"
        ).fetchall()
        pending = sum(1 for t in conn.execute(db.TAG_SELECT) if hudu_needs_sync(t))
    finally:
        conn.close()
    return page(request, "clients.html", clients=rows, hudu_pending=pending, hudu_write=hudu.write_enabled())


@app.post("/clients/sync")
def clients_sync(request: Request):
    auth.require_admin(request)
    try:
        companies = hudu.list_companies()
    except hudu.HuduError as e:
        flash(request, str(e), "bad")
        return go("/clients")
    conn = db.connect()
    added = renamed = 0
    try:
        for co in companies:
            if co.get("archived"):
                continue
            hid, name = co.get("id"), (co.get("name") or "").strip()
            if not hid or not name:
                continue
            row = conn.execute("SELECT * FROM clients WHERE hudu_id = ?", (hid,)).fetchone()
            if row:
                if row["name"] != name:
                    conn.execute("UPDATE clients SET name=?, updated_at=? WHERE id=?", (name, db.now(), row["id"]))
                    renamed += 1
            else:
                conn.execute(
                    "INSERT INTO clients(hudu_id, name, label_name, is_internal, active, updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (hid, name, (co.get("nickname") or name).strip()[:40], 0, 1, db.now()),
                )
                added += 1
    finally:
        conn.close()
    flash(request, f"Synced {len(companies)} Hudu companies: {added} added, {renamed} renamed. "
                   "Check the label names of new clients.")
    return go("/clients")


@app.post("/clients/new")
def client_new(request: Request, name: str = Form(...), label_name: str = Form("")):
    auth.require_admin(request)
    name = clean(name, 120)
    if name:
        conn = db.connect()
        try:
            conn.execute(
                "INSERT INTO clients(hudu_id, name, label_name, is_internal, active, updated_at) VALUES(NULL,?,?,0,1,?)",
                (name, clean(label_name, 40) or name[:40], db.now()),
            )
        finally:
            conn.close()
        flash(request, f"Added {name}.")
    return go("/clients")


@app.post("/clients/{cid}")
def client_update(request: Request, cid: int, label_name: str = Form(...), active: str = Form("")):
    auth.require_admin(request)
    label = clean(label_name, 40)
    if not label:
        flash(request, "The label name can't be empty.", "bad")
        return go("/clients")
    conn = db.connect()
    try:
        conn.execute("UPDATE clients SET label_name=?, active=?, updated_at=? WHERE id=?",
                     (label, 1 if active else 0, db.now(), cid))
    finally:
        conn.close()
    flash(request, "Client saved. Tags already printed with the old name are listed as needing a reprint.")
    return go("/clients")


# ---------- users ----------

@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    auth.require_admin(request)
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE").fetchall()
    finally:
        conn.close()
    return page(request, "users.html", users=rows)


@app.post("/users/new")
def user_new(request: Request, username: str = Form(...), email: str = Form(""), role: str = Form("user"),
             password: str = Form("")):
    auth.require_admin(request)
    username = clean(username, 80)
    if role not in ("user", "admin") or not username:
        return go("/users")
    if password and len(password) < 12:
        flash(request, "Passwords need at least 12 characters.", "bad")
        return go("/users")
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO users(username, email, role, password, created_at) VALUES(?,?,?,?,?)",
            (username, clean(email, 200).lower(), role, auth.hash_password(password) if password else None, db.now()),
        )
        flash(request, f"Added {username}." + ("" if password else " They can only sign in with Microsoft."))
    except Exception:
        flash(request, f"{username} already exists.", "bad")
    finally:
        conn.close()
    return go("/users")


def _admin_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]


@app.post("/users/{uid}/role")
def user_role(request: Request, uid: int, role: str = Form(...)):
    me = auth.require_admin(request)
    if role not in ("user", "admin"):
        return go("/users")
    conn = db.connect()
    try:
        if role == "user" and uid == me["id"]:
            flash(request, "You can't remove your own admin role.", "bad")
        elif role == "user" and _admin_count(conn) <= 1:
            flash(request, "There must be at least one admin.", "bad")
        else:
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
            flash(request, "Role updated.")
    finally:
        conn.close()
    return go("/users")


@app.post("/users/{uid}/password")
def user_reset(request: Request, uid: int, password: str = Form(...)):
    auth.require_admin(request)
    if len(password) < 12:
        flash(request, "Passwords need at least 12 characters.", "bad")
        return go("/users")
    conn = db.connect()
    try:
        conn.execute("UPDATE users SET password = ? WHERE id = ?", (auth.hash_password(password), uid))
    finally:
        conn.close()
    flash(request, "Password set.")
    return go("/users")


@app.post("/users/{uid}/delete")
def user_delete(request: Request, uid: int):
    me = auth.require_admin(request)
    if uid == me["id"]:
        flash(request, "You can't delete your own account.", "bad")
        return go("/users")
    conn = db.connect()
    try:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    finally:
        conn.close()
    flash(request, "User deleted.")
    return go("/users")


@app.get("/account", response_class=HTMLResponse)
def account(request: Request):
    auth.require_user(request)
    return page(request, "account.html")


@app.post("/account/name")
def account_name(request: Request, name: str = Form("")):
    user = auth.require_user(request)
    name = clean(name, 80)
    conn = db.connect()
    try:
        conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (name, user["id"]))
    finally:
        conn.close()
    flash(request, f"Name changed to {name}." if name else "Name cleared; your username is shown instead.")
    return go("/account")


@app.post("/users/{uid}/name")
def user_name(request: Request, uid: int, name: str = Form("")):
    auth.require_admin(request)
    conn = db.connect()
    try:
        conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (clean(name, 80), uid))
    finally:
        conn.close()
    flash(request, "Name saved.")
    return go("/users")


@app.post("/account/password")
def account_password(request: Request, current: str = Form(""), new: str = Form(...)):
    user = auth.require_user(request)
    conn = db.connect()
    try:
        row = conn.execute("SELECT password FROM users WHERE id = ?", (user["id"],)).fetchone()
        if row["password"] and not auth.verify_password(current, row["password"]):
            flash(request, "Your current password is wrong.", "bad")
        elif len(new) < 12:
            flash(request, "Passwords need at least 12 characters.", "bad")
        else:
            conn.execute("UPDATE users SET password = ? WHERE id = ?", (auth.hash_password(new), user["id"]))
            flash(request, "Password changed.")
    finally:
        conn.close()
    return go("/account")


# ---------- Hudu lookup ----------

@app.get("/api/hudu/assets")
def hudu_assets(request: Request, client_id: int, q: str = ""):
    auth.require_user(request)
    conn = db.connect()
    try:
        c = conn.execute("SELECT hudu_id FROM clients WHERE id = ?", (client_id,)).fetchone()
    finally:
        conn.close()
    if not c or not c["hudu_id"]:
        return JSONResponse({"error": "This client isn't linked to a Hudu company."}, status_code=400)
    try:
        return {"assets": hudu.search_assets(c["hudu_id"], q.strip()[:80])}
    except hudu.HuduError as e:
        return JSONResponse({"error": str(e)}, status_code=502)


# ---------- scan landing (must stay last) ----------

@app.get("/{raw}", response_class=HTMLResponse)
def scan(request: Request, raw: str):
    code = codes.parse(raw)
    if not code:
        return page(request, "message.html", status_code=404, heading="Page not found",
                    body="Check the address and try again.")
    user = getattr(request.state, "user", None)
    conn = db.connect()
    try:
        tag = db.get_tag(conn, code)
    finally:
        conn.close()
    if user:
        return go(f"/tags/{code}")
    org = settings.get_all()
    return page(request, "public.html", status_code=200 if tag else 404, tag=tag, code=code,
                support_phone=org["support_phone"], support_tel=tel_link(org["support_phone"]),
                support_email=org["support_email"], org=org,
                owner=(label_name(tag, org) if tag and tag["format"] != "loan" else None))
