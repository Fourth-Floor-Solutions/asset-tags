"""API keys for other systems (issue #42).

A key is `td_` plus 32 random bytes (URL-safe). Only its SHA-256 hash is
stored, with the first characters kept to tell keys apart, so a key is
shown once when it's created and can't be recovered. Keys are read-only
or read/write, can be revoked at once, and record when they were last used.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict, deque

from . import db

SCOPES = {"read": "Read only", "write": "Read and write"}
PER_MINUTE = 120
_hits: defaultdict[int, deque[float]] = defaultdict(deque)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create(conn, name: str, scope: str, actor: str) -> str:
    key = "td_" + secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO api_keys(name, scope, key_hash, prefix, created_by, created_at) VALUES(?,?,?,?,?,?)",
        (name, scope, _hash(key), key[:10], actor, db.now()),
    )
    return key


def revoke(conn, key_id: int) -> None:
    conn.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (db.now(), key_id))


def all_keys(conn) -> list:
    return conn.execute("SELECT * FROM api_keys ORDER BY revoked_at IS NOT NULL, created_at DESC").fetchall()


def lookup(conn, key: str):
    """The active key row for a presented key, or None. Constant-time compare on the hash."""
    if not key.startswith("td_"):
        return None
    h = _hash(key)
    row = conn.execute("SELECT * FROM api_keys WHERE key_hash=? AND revoked_at IS NULL", (h,)).fetchone()
    if row and secrets.compare_digest(row["key_hash"], h):
        conn.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (db.now(), row["id"]))
        return row
    return None


def rate_limited(key_id: int) -> bool:
    hits = _hits[key_id]
    now = time.monotonic()
    while hits and hits[0] < now - 60:
        hits.popleft()
    if len(hits) >= PER_MINUTE:
        return True
    hits.append(now)
    return False
