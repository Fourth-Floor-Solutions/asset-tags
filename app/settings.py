"""Settings, edited by admins on the Settings page.

Everything that makes the app "ours" lives here rather than in code: the
organisation's name and mark, label wording, accent colour, support contact,
Hudu connection, Microsoft sign-in and the source-code link.

Two kinds of environment link:
- ENV_FALLBACK: the environment variable is used until the setting is saved.
- ENV_WINS: if the environment variable is set, it's used and the setting
  can't be edited in the app (so existing .env-based installs keep working).

Secrets (SECRET_KEYS) are stored encrypted (see secretbox) and never shown.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from . import db, log

_log = log.get("settings")

DEFAULTS = {
    "org_name": "Your organisation",
    "org_short": "Your organisation",
    "org_mark": "TD",
    "accent": "#5b8def",
    "support_phone": "",
    "support_email": "",
    "report_button": "Report a problem",
    "report_subject": "Problem with {code} ({device}, {client})",
    "label_managed": "Managed by {name}",
    "label_loan_top": "{SHORT} · LOAN",
    "label_loan_bottom": "Please return to {name}",
    "internal_client_id": "",
    "hudu_url": "",
    "hudu_write": "0",
    "hudu_tag_field": "Asset Tag",
    "hudu_coverage_exclude": "",  # Hudu layout ids left out of tag coverage, comma-separated
    "sso_auto_domains": "",
    "entra_tenant_id": "",
    "entra_client_id": "",
    "entra_secret_expires": "",  # YYYY-MM-DD, for the expiry warning
    "source_url": "https://github.com/Fourth-Floor-Solutions/tagdesk",
}

# Setting -> environment variable that, when set, overrides it.
ENV_WINS = {
    "entra_tenant_id": "ENTRA_TENANT_ID",
    "entra_client_id": "ENTRA_CLIENT_ID",
    "source_url": "SOURCE_URL",
    "hudu_api_key": "HUDU_API_KEY",
    "entra_client_secret": "ENTRA_CLIENT_SECRET",
}
SECRET_KEYS = ("hudu_api_key", "entra_client_secret")

# Setting -> environment variable used until the setting is saved.
ENV_FALLBACK = {
    "support_phone": "SUPPORT_PHONE",
    "support_email": "SUPPORT_EMAIL",
    "hudu_url": "HUDU_URL",
    "hudu_write": "HUDU_WRITE",
    "hudu_tag_field": "HUDU_TAG_FIELD",
    "sso_auto_domains": "ENTRA_AUTO_DOMAINS",
}

KEYS = tuple(DEFAULTS)

# Per-request cache: a page reads settings many times (org details, Hudu, sign-in, label
# text), so within request_scope() each value is read from the database once. Outside a
# request (startup, scripts, tests calling functions directly) every read goes to the database.
_cache: ContextVar[dict | None] = ContextVar("settings_cache", default=None)


@contextmanager
def request_scope() -> Iterator[None]:
    token = _cache.set({})
    try:
        yield
    finally:
        _cache.reset(token)


def _cached(key: str, load):
    cache = _cache.get()
    if cache is None:
        return load()
    if key not in cache:
        cache[key] = load()
    return cache[key]


def _changed() -> None:
    cache = _cache.get()
    if cache is not None:
        cache.clear()


HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def get_all() -> dict:
    return dict(_cached("all", _load_all))


def _load_all() -> dict:
    with db.connection() as conn:
        saved = {
            r["key"]: r["value"]
            for r in conn.execute(f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(KEYS))})", KEYS)
        }
    out = {}
    for k, default in DEFAULTS.items():
        if k in ENV_WINS and os.environ.get(ENV_WINS[k], "").strip():
            out[k] = os.environ[ENV_WINS[k]].strip()
        elif k in saved:
            out[k] = saved[k]
        elif k in ENV_FALLBACK and os.environ.get(ENV_FALLBACK[k]):
            out[k] = os.environ[ENV_FALLBACK[k]].strip()
        else:
            out[k] = default
    return out


def get(key: str) -> str:
    return get_all()[key]


def save(values: dict) -> None:
    _changed()
    with db.connection() as conn:
        for k, v in values.items():
            if k in DEFAULTS:
                db.set_setting(conn, k, str(v))


def setup_done() -> bool:
    with db.connection() as conn:
        return db.get_setting(conn, "setup_done") == "1"


def mark_setup_done() -> None:
    _changed()
    with db.connection() as conn:
        db.set_setting(conn, "setup_done", "1")


def ink_for(accent: str) -> str:
    """Black or white text, whichever reads better on the accent colour."""
    try:
        r, g, b = (int(accent[i : i + 2], 16) / 255 for i in (1, 3, 5))
    except (ValueError, TypeError):
        return "#16181c"

    def lin(c: float) -> float:  # sRGB channel to linear light
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    luminance = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    return "#16181c" if luminance > 0.25 else "#ffffff"


def fill(template: str, s: dict) -> str:
    """Label wording with {name}, {short} and {SHORT} filled in."""
    return (
        template.replace("{name}", s["org_name"])
        .replace("{short}", s["org_short"])
        .replace("{SHORT}", s["org_short"].upper())
    )


def label_text(s: dict) -> dict:
    """The organisation-specific wording that goes on labels."""
    return {
        "managed": fill(s["label_managed"], s),
        "loan_top": fill(s["label_loan_top"], s),
        "loan_bottom": fill(s["label_loan_bottom"], s),
        "loan_name": s["org_short"],
        "mark": (s["org_mark"] or "").strip()[:3],
    }


def formats() -> dict:
    """Saved label-format changes: {format: {enabled, tape_colour, tape}}."""
    import json

    with db.connection() as conn:
        raw = db.get_setting(conn, "label_formats")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def save_formats(overrides: dict) -> None:
    _changed()
    import json

    with db.connection() as conn:
        db.set_setting(conn, "label_formats", json.dumps(overrides))


def source(key: str) -> str:
    """Where a setting's value comes from: "env", "saved" or "default"."""
    if key in ENV_WINS and os.environ.get(ENV_WINS[key], "").strip():
        return "env"
    with db.connection() as conn:
        return "saved" if db.get_setting(conn, key) not in (None, "") else "default"


# ---------- secrets ----------


def secret_info(key: str) -> dict:
    """{"status": env | set | unset | unreadable, "value": str or None, "error": str}."""
    return dict(_cached(f"secret:{key}", lambda: _load_secret(key)))


def _load_secret(key: str) -> dict:
    from . import secretbox

    env = os.environ.get(ENV_WINS.get(key, ""), "").strip() if key in ENV_WINS else ""
    if env:
        return {"status": "env", "value": env, "error": ""}
    with db.connection() as conn:
        stored = db.get_setting(conn, key)
    if not stored:
        return {"status": "unset", "value": None, "error": ""}
    try:
        return {"status": "set", "value": secretbox.decrypt(stored), "error": ""}
    except secretbox.SecretUnreadable as e:
        return {"status": "unreadable", "value": None, "error": str(e)}


def secret(key: str):
    return secret_info(key)["value"]


def save_secret(key: str, value) -> None:
    """Store a secret encrypted, or remove it when value is None or empty."""
    _changed()
    from . import secretbox

    with db.connection() as conn:
        if value:
            db.set_setting(conn, key, secretbox.encrypt(value))
        else:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))


# ---------- audit ----------


def audit(actor: str, setting: str, change: str = "") -> None:
    _log.info("%s changed by %s%s", setting, actor, f": {change[:300]}" if change else "")
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO audit(at, actor, setting, change) VALUES(?,?,?,?)", (db.now(), actor, setting, change[:300])
        )


def recent_audit(limit: int = 15) -> list:
    with db.connection() as conn:
        return conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
