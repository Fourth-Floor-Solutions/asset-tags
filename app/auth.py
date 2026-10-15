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

from fastapi import Request

from . import db

ITERATIONS = 310_000
GUID_RE = __import__("re").compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Tenant values that accept accounts from any organisation; never allowed.
MULTI_TENANT = ("common", "organizations", "consumers")


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS).hex()
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, iters, salt, digest = stored.split("$")
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters)).hex()
    return hmac.compare_digest(check, digest)


# A real hash of a random password, checked when the username doesn't exist so
# the response takes the same time either way.
DUMMY_HASH = hash_password(secrets.token_hex(16))


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


def sso_config():
    """(tenant id, client id, client secret) from Settings or .env; None if incomplete or unreadable."""
    from . import settings

    s = settings.get_all()
    secret = settings.secret("entra_client_secret")
    tenant, client = s["entra_tenant_id"].strip(), s["entra_client_id"].strip()
    if tenant.lower() in MULTI_TENANT:
        return None  # refuse: would let other organisations' accounts in
    return (tenant, client, secret) if tenant and client and secret else None


def sso_enabled() -> bool:
    return sso_config() is not None


_oauth = None
_oauth_for = None


def metadata_url(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration"


def oauth():
    """The Microsoft OAuth client, rebuilt whenever the sign-in settings change."""
    global _oauth, _oauth_for
    cfg = sso_config()
    if cfg is None:
        _oauth = _oauth_for = None
        return None
    if _oauth is None or _oauth_for != cfg:
        from authlib.integrations.starlette_client import OAuth

        tenant, client_id, secret = cfg
        client = OAuth()
        client.register(
            "entra",
            client_id=client_id,
            client_secret=secret,
            server_metadata_url=metadata_url(tenant),
            client_kwargs={"scope": "openid email profile"},
        )
        _oauth, _oauth_for = client, cfg
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
