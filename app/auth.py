"""Local accounts (PBKDF2) with signed session cookies, plus optional Entra ID SSO.

Same model as Script Scheduler: roles are `admin` (clients, users) and `user`
(tag devices, print). SSO matches an existing user by email; unknown emails are
refused unless their domain is listed in Settings (auto-enrol domains).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Optional

from fastapi import Request

from . import db

ITERATIONS = 310_000


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS).hex()
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored:
        return False
    try:
        _, iters, salt, digest = stored.split("$")
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters)).hex()
    return hmac.compare_digest(check, digest)


def seed_admin() -> None:
    """Create `admin` on first boot from ADMIN_PASSWORD if there are no users."""
    conn = db.connect()
    try:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        pw = os.environ.get("ADMIN_PASSWORD")
        if not pw:
            return
        conn.execute(
            "INSERT INTO users(username, email, role, password, created_at) VALUES(?,?,?,?,?)",
            ("admin", os.environ.get("ADMIN_EMAIL", ""), "admin", hash_password(pw), db.now()),
        )
    finally:
        conn.close()


def session_user(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return None
    conn = db.connect()
    try:
        return conn.execute("SELECT id, username, email, role, display_name FROM users WHERE id = ?", (uid,)).fetchone()
    finally:
        conn.close()


def require_user(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise LoginRequired()
    return user


def require_admin(request: Request):
    user = require_user(request)
    if user["role"] != "admin":
        raise Forbidden()
    return user


def sso_enabled() -> bool:
    return all(os.environ.get(k) for k in ("ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET"))


_oauth = None


def oauth():
    global _oauth
    if _oauth is None and sso_enabled():
        from authlib.integrations.starlette_client import OAuth

        _oauth = OAuth()
        _oauth.register(
            "entra",
            client_id=os.environ["ENTRA_CLIENT_ID"],
            client_secret=os.environ["ENTRA_CLIENT_SECRET"],
            server_metadata_url=(
                f"https://login.microsoftonline.com/{os.environ['ENTRA_TENANT_ID']}"
                "/v2.0/.well-known/openid-configuration"
            ),
            client_kwargs={"scope": "openid email profile"},
        )
    return _oauth


def label(user) -> str:
    """How a user is shown and recorded in history: their name, else username."""
    return (user["display_name"] or "").strip() or user["username"]


def sso_user_for(email: str, name: str = ""):
    """Find (or, for allowed domains, create) the user for an SSO email."""
    email = (email or "").strip().lower()
    if not email:
        return None
    conn = db.connect()
    try:
        name = (name or "").strip()[:80]
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            if name and not (row["display_name"] or "").strip():
                conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (name, row["id"]))
                row = conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
            return row
        from . import settings

        domains = [d.strip().lower() for d in settings.get("sso_auto_domains").split(",") if d.strip()]
        if email.split("@")[-1] not in domains:
            return None
        conn.execute(
            "INSERT INTO users(username, email, role, password, created_at, display_name) VALUES(?,?,?,?,?,?)",
            (email, email, "user", None, db.now(), name),
        )
        return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    finally:
        conn.close()
