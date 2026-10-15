"""Backups of the SQLite database (issue #41).

`snapshot()` makes a consistent copy while the app is running (SQLite's
backup API). Admins download one from Settings; a daily copy is also kept
in the data folder's `backups/` (BACKUP_KEEP copies, default 7; 0 = off).
Saved secrets inside are encrypted with `secret.key` (or
TAGDESK_SECRET_KEY), which is not in the database, so keep that too.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
from datetime import UTC, datetime

from . import db, log

_log = log.get("backup")
_stop = threading.Event()


def keep() -> int:
    raw = os.environ.get("BACKUP_KEEP", "7").strip()
    return int(raw) if raw.isdigit() else 7


def snapshot(dest: str) -> None:
    src = db.connect()
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def to_bytes() -> bytes:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        snapshot(path)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.remove(path)


def folder() -> str:
    return os.path.join(db.DATA_DIR, "backups")


def daily() -> str | None:
    """Today's copy in backups/ (once a day), pruning to the newest keep() copies."""
    n = keep()
    if not n:
        return None
    os.makedirs(folder(), exist_ok=True)
    path = os.path.join(folder(), f"tagdesk-{datetime.now(UTC):%Y-%m-%d}.db")
    if not os.path.exists(path):
        snapshot(path)
        os.chmod(path, 0o600)
        _log.info("daily backup written: %s", os.path.basename(path))
    copies = sorted(f for f in os.listdir(folder()) if f.startswith("tagdesk-") and f.endswith(".db"))
    for old in copies[:-n]:
        os.remove(os.path.join(folder(), old))
    return path


def list_daily() -> list[dict]:
    if not os.path.isdir(folder()):
        return []
    out = []
    for f in sorted(os.listdir(folder()), reverse=True):
        if f.startswith("tagdesk-") and f.endswith(".db"):
            out.append({"name": f, "kb": max(1, os.path.getsize(os.path.join(folder(), f)) // 1024)})
    return out


def _loop() -> None:
    wait = 60.0  # first copy a minute after startup, then every 6 hours (one file per day)
    while not _stop.wait(wait):
        wait = 6 * 3600
        try:
            daily()
        except Exception:
            _log.exception("daily backup failed")


def start() -> None:
    if keep():
        _stop.clear()
        threading.Thread(target=_loop, name="daily-backup", daemon=True).start()


def stop() -> None:
    _stop.set()
