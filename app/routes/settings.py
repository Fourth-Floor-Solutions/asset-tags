"""The Settings page, first-run setup, and the Hudu and sign-in tests."""

from __future__ import annotations

import os
import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from .. import apikeys, auth, backup, db, hudu, labels, printer, secretbox, settings
from ..tagops import (
    clients,
    label_text,
)
from ..web import PUBLIC_BASE_URL, clean, flash, go, page

router = APIRouter()


GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{1,252}$")


@router.post("/settings/secrets/{key}/import")
def secret_import(request: Request, key: str):
    """Copy a secret that's currently set in .env into Settings (encrypted)."""
    user = auth.require_admin(request)
    if key not in settings.SECRET_KEYS:
        return go("/settings")
    info = settings.secret_info(key)
    if info["status"] != "env":
        flash(request, "That value isn't set in .env.", "bad")
        return go("/settings")
    settings.save_secret(key, info["value"])
    settings.audit(auth.label(user), key, "copied from .env into Settings")
    env_name = settings.ENV_WINS[key]
    flash(
        request,
        f"Copied into Settings. Remove {env_name} from the server's .env and restart the app to manage it here; "
        f"until then the .env value is still used.",
    )
    return go("/settings#integrations")


@router.post("/settings/test/hudu")
def settings_test_hudu(request: Request):
    auth.require_admin(request)
    try:
        n = len(hudu.list_companies())
        flash(request, f"Hudu connection works: {n} compan{'y' if n == 1 else 'ies'} found.")
    except hudu.HuduError as e:
        flash(request, f"Hudu connection failed: {e}", "bad")
    return go("/settings#integrations")


@router.post("/settings/test/sso")
def settings_test_sso(request: Request):
    auth.require_admin(request)
    cfg = auth.sso_config()
    if cfg is None:
        flash(
            request, "Microsoft sign-in isn't fully set up: it needs the tenant ID, client ID and client secret.", "bad"
        )
        return go("/settings#integrations")
    import httpx

    try:
        r = httpx.get(auth.metadata_url(cfg[0]), timeout=10)
        ok = r.status_code == 200 and "authorization_endpoint" in r.json()
    except (httpx.HTTPError, ValueError):
        ok = False
    if ok:
        flash(
            request,
            "Microsoft sign-in settings look right: the tenant was found. Sign out and use "
            "Sign in with Microsoft to check the client ID and secret.",
        )
    else:
        flash(request, "Microsoft couldn't find that tenant. Check the tenant ID.", "bad")
    return go("/settings#integrations")


def _settings_page(request: Request, setup: bool, error: str = "", form: dict | None = None):
    with db.connection() as conn:
        cl = clients(conn)
        internal = conn.execute("SELECT id FROM clients WHERE is_internal = 1 LIMIT 1").fetchone()
    s = form or settings.get_all()
    if not s.get("internal_client_id") and internal:
        s = dict(s, internal_client_id=str(internal["id"]))
    return page(
        request,
        "settings.html",
        status_code=400 if error else 200,
        setup=setup,
        s=s,
        error=error,
        tape_colours=list(labels.TAPE_FILLS),
        clients=cl,
        preview=label_text(s) if not error else None,
        env={"public_url": PUBLIC_BASE_URL, "sso": auth.sso_enabled(), "printer_count": len(printers_live_rows())},
        src={k: settings.source(k) for k in settings.ENV_WINS},
        env_names=settings.ENV_WINS,
        secrets={k: settings.secret_info(k) for k in settings.SECRET_KEYS},
        audit=settings.recent_audit(),
        backups=backup.list_daily(),
        api_keys=_api_keys(),
        api_scopes=apikeys.SCOPES,
        backup_keep=backup.keep(),
        key_in_env=bool(os.environ.get("TAGDESK_SECRET_KEY", "").strip()),
    )


def _api_keys() -> list:
    with db.connection() as conn:
        return apikeys.all_keys(conn)


@router.post("/settings/api-keys", response_class=HTMLResponse)
def api_key_create(request: Request, name: str = Form(""), scope: str = Form("read")):
    user = auth.require_admin(request)
    name = clean(name, 60)
    if not name or scope not in apikeys.SCOPES:
        flash(request, "Give the key a name and choose what it can do.", "bad")
        return go("/settings#api-keys")
    with db.connection() as conn:
        key = apikeys.create(conn, name, scope, auth.label(user))
    settings.audit(auth.label(user), "api key", f"created {name!r} ({apikeys.SCOPES[scope].lower()})")
    # Shown on this page only, never stored or put in the session.
    return page(request, "api_key_created.html", key_name=name, scope=apikeys.SCOPES[scope], key=key)


@router.post("/settings/api-keys/{key_id}/revoke")
def api_key_revoke(request: Request, key_id: int):
    user = auth.require_admin(request)
    with db.connection() as conn:
        row = conn.execute("SELECT name FROM api_keys WHERE id=?", (key_id,)).fetchone()
        if row:
            apikeys.revoke(conn, key_id)
    if row:
        settings.audit(auth.label(user), "api key", f"revoked {row['name']!r}")
        flash(request, f"Revoked {row['name']}. It stops working immediately.")
    return go("/settings#api-keys")


@router.get("/settings/backup/tagdesk.db")
def backup_download(request: Request):
    user = auth.require_admin(request)
    data = backup.to_bytes()
    settings.audit(auth.label(user), "backup", "downloaded the database")
    return Response(
        data,
        media_type="application/vnd.sqlite3",
        headers={
            "Content-Disposition": f'attachment; filename="tagdesk-{db.now()[:10]}.db"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/settings/backup/secret.key")
def backup_key_download(request: Request):
    """The key that decrypts saved secrets, kept outside the database. Not offered when it's set in .env."""
    user = auth.require_admin(request)
    if os.environ.get("TAGDESK_SECRET_KEY", "").strip():
        return go("/settings#backup")
    secretbox.encrypt("")  # makes sure the key file exists
    with open(os.path.join(db.DATA_DIR, secretbox.KEY_FILE), "rb") as f:
        data = f.read()
    settings.audit(auth.label(user), "backup", "downloaded secret.key")
    return Response(
        data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="secret.key"', "Cache-Control": "no-store"},
    )


def printers_live_rows() -> list:
    with db.connection() as conn:
        return printer.all_printers(conn)


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    auth.require_admin(request)
    if settings.setup_done():
        return go("/settings")
    return _settings_page(request, setup=True)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    auth.require_admin(request)
    return _settings_page(request, setup=False)


@router.post("/settings")
async def settings_save(request: Request):
    user = auth.require_admin(request)
    # Text fields only: this form has no file uploads.
    data = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    setup = data.get("setup") == "1"
    before = settings.get_all()
    # Fields not on the form keep their current value; values set in .env are never copied in.
    v = {k: (clean(data.get(k, ""), 200) if k in data else before[k]) for k in settings.KEYS}
    if "hudu_url" in data:
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
    elif v["entra_tenant_id"] and not TENANT.match(v["entra_tenant_id"]):
        error = "The tenant ID should be a GUID or a domain like example.onmicrosoft.com."
    elif v["entra_tenant_id"].lower() in auth.MULTI_TENANT:
        error = "Use your own tenant ID, not 'common', 'organizations' or 'consumers': those let other organisations' accounts sign in."
    elif v["entra_client_id"] and not GUID.match(v["entra_client_id"]):
        error = "The client ID (application ID) should be a GUID like 00000000-0000-0000-0000-000000000000."
    elif v["entra_secret_expires"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v["entra_secret_expires"]):
        error = "Enter the secret's expiry as a date."
    elif v["source_url"] and not v["source_url"].startswith(("https://", "http://")):
        error = "The source code link must start with https://"
    v["org_short"] = v["org_short"] or v["org_name"]
    # Format settings only change when the form included the format table.
    formats_shown = data.get("formats_shown") == "1"
    fmt_over = {}
    for key, base in labels.BUILT_IN.items() if formats_shown else []:
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
    with db.connection() as conn:
        # Your organisation's own client (used for loan tags). Create one if asked or if none exists.
        cid = v["internal_client_id"]
        if cid == "new" or (not cid and not conn.execute("SELECT 1 FROM clients WHERE is_internal = 1").fetchone()):
            conn.execute(
                "INSERT INTO clients(hudu_id, name, label_name, is_internal, active, updated_at) "
                "VALUES(NULL,?,?,1,1,?)",
                (v["org_name"], v["org_short"][:40], db.now()),
            )
            cid = str(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        if cid.isdigit():
            conn.execute("UPDATE clients SET is_internal = CASE WHEN id = ? THEN 1 ELSE 0 END", (int(cid),))
        v["internal_client_id"] = cid if cid.isdigit() else ""
    env_set = {k for k in settings.ENV_WINS if settings.source(k) == "env"}
    if (
        v["hudu_url"] != before["hudu_url"]
        and before["hudu_url"]
        and "hudu_api_key" not in env_set
        and settings.secret_info("hudu_api_key")["status"] == "set"
        and not (data.get("hudu_api_key") or "").strip()
    ):
        # Don't send a saved key to a different server without the admin re-entering it.
        return _settings_page(
            request, setup, error="You changed the Hudu address: enter the API key again too.", form=v
        )
    to_save = {k: val for k, val in v.items() if k not in env_set}
    to_save["source_url"] = to_save.get("source_url") or settings.DEFAULTS["source_url"]
    settings.save(to_save)
    actor = auth.label(user)
    for k, val in to_save.items():
        if str(before.get(k, "")) != str(val):
            settings.audit(actor, k, f"{str(before.get(k, ''))[:60]!r} → {str(val)[:60]!r}")
    # Secrets: write-only. Empty keeps the current value; the clear box removes it.
    for key in settings.SECRET_KEYS:
        if key in env_set:
            continue
        new = (data.get(key) or "").strip()
        if data.get(f"clear_{key}"):
            settings.save_secret(key, None)
            settings.audit(actor, key, "cleared")
        elif new:
            settings.save_secret(key, new[:500])
            settings.audit(actor, key, "changed")
    if formats_shown:
        settings.save_formats(fmt_over)
        labels.apply_overrides(fmt_over)
    if setup:
        settings.mark_setup_done()
        flash(request, "Setup saved. Next, add your label printer, then sync or add your clients.")
        return go("/printer")
    flash(request, "Settings saved.")
    return go("/settings")
