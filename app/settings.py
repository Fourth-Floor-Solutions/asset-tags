"""Organisation settings, edited by admins on the Settings page.

Everything that makes the app "ours" lives here rather than in code: the
organisation's name and mark, label wording, accent colour, support contact,
Hudu options and SSO auto-enrol domains. Secrets (HUDU_API_KEY, the SSO client
secret) stay in the environment.

A few settings fall back to an environment variable when they haven't been
saved yet, so an existing .env keeps working.
"""
from __future__ import annotations

import os
import re

from . import db

DEFAULTS = {
    "org_name": "Your organisation",
    "org_short": "Your organisation",
    "org_mark": "AT",
    "accent": "#5b8def",
    "support_phone": "",
    "support_email": "",
    "label_managed": "Managed by {name}",
    "label_loan_top": "{SHORT} · LOAN",
    "label_loan_bottom": "Please return to {name}",
    "internal_client_id": "",
    "hudu_url": "",
    "hudu_write": "0",
    "hudu_tag_field": "Asset Tag",
    "sso_auto_domains": "",
}

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
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def get_all() -> dict:
    conn = db.connect()
    try:
        saved = {r["key"]: r["value"] for r in conn.execute(
            f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(KEYS))})", KEYS)}
    finally:
        conn.close()
    out = {}
    for k, default in DEFAULTS.items():
        if k in saved:
            out[k] = saved[k]
        elif k in ENV_FALLBACK and os.environ.get(ENV_FALLBACK[k]):
            out[k] = os.environ[ENV_FALLBACK[k]].strip()
        else:
            out[k] = default
    return out


def get(key: str) -> str:
    return get_all()[key]


def save(values: dict) -> None:
    conn = db.connect()
    try:
        for k, v in values.items():
            if k in DEFAULTS:
                db.set_setting(conn, k, str(v))
    finally:
        conn.close()


def setup_done() -> bool:
    conn = db.connect()
    try:
        return db.get_setting(conn, "setup_done") == "1"
    finally:
        conn.close()


def mark_setup_done() -> None:
    conn = db.connect()
    try:
        db.set_setting(conn, "setup_done", "1")
    finally:
        conn.close()


def ink_for(accent: str) -> str:
    """Black or white text, whichever reads better on the accent colour."""
    try:
        r, g, b = (int(accent[i:i + 2], 16) / 255 for i in (1, 3, 5))
    except (ValueError, TypeError):
        return "#16181c"
    lin = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    luminance = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    return "#16181c" if luminance > 0.25 else "#ffffff"


def fill(template: str, s: dict) -> str:
    """Label wording with {name}, {short} and {SHORT} filled in."""
    return (template.replace("{name}", s["org_name"])
                    .replace("{short}", s["org_short"])
                    .replace("{SHORT}", s["org_short"].upper()))


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

    conn = db.connect()
    try:
        raw = db.get_setting(conn, "label_formats")
    finally:
        conn.close()
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def save_formats(overrides: dict) -> None:
    import json

    conn = db.connect()
    try:
        db.set_setting(conn, "label_formats", json.dumps(overrides))
    finally:
        conn.close()
