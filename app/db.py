"""SQLite storage. One file in DATA_DIR; every request opens its own connection."""
from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from . import codes

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
DB_PATH = os.path.join(DATA_DIR, "tagdesk.db")
LEGACY_DB_PATH = os.path.join(DATA_DIR, "asset-tags.db")   # name used before the app became TagDesk

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  email TEXT COLLATE NOCASE,
  role TEXT NOT NULL DEFAULT 'user',
  password TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
  id INTEGER PRIMARY KEY,
  hudu_id INTEGER UNIQUE,
  name TEXT NOT NULL,
  label_name TEXT NOT NULL,
  is_internal INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
  code TEXT PRIMARY KEY,
  num INTEGER NOT NULL UNIQUE,
  client_id INTEGER NOT NULL REFERENCES clients(id),
  format TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'unassigned',   -- unassigned | active | retired
  device_name TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  serial TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  hudu_asset_id INTEGER,
  hudu_asset_url TEXT NOT NULL DEFAULT '',
  printed_count INTEGER NOT NULL DEFAULT 0,
  printed_as TEXT,                          -- format|name as last printed
  printed_at TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tags_client ON tags(client_id);
CREATE INDEX IF NOT EXISTS tags_serial ON tags(serial);

CREATE TABLE IF NOT EXISTS printers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  host TEXT NOT NULL,
  nudge_dots INTEGER NOT NULL DEFAULT 0,   -- vertical calibration, negative = down
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cassettes (
  id INTEGER PRIMARY KEY,
  printer_id INTEGER NOT NULL,
  tape TEXT NOT NULL,                    -- e.g. "18|yellow"
  used_cm INTEGER NOT NULL DEFAULT 0,
  length_cm INTEGER NOT NULL DEFAULT 800,  -- standard TZe cassette: 8 m
  estimated INTEGER NOT NULL DEFAULT 1,    -- 1 = tracking started part-way through
  fitted_at TEXT NOT NULL,
  UNIQUE(printer_id, tape)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_code ON events(code);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(DB_PATH) and os.path.exists(LEGACY_DB_PATH):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(LEGACY_DB_PATH + suffix):
                os.rename(LEGACY_DB_PATH + suffix, DB_PATH + suffix)
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init() -> None:
    conn = connect()
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _add_columns(conn, "users", {
        "display_name": "TEXT NOT NULL DEFAULT ''",
    })
    _add_columns(conn, "printers", {
        "chain": "INTEGER NOT NULL DEFAULT 0",      # unused: chain printing was removed (labels got stuck)
        "last_counter_cm": "INTEGER",               # printer's lifetime print length at last read
        "last_tape": "TEXT",                         # tape loaded at last read, e.g. "18|yellow"
    })
    _add_columns(conn, "tags", {
        "hudu_pushed_asset_id": "INTEGER",          # asset whose Asset Tag field holds this code
        "hudu_sync_status": "TEXT NOT NULL DEFAULT ''",
        "hudu_sync_at": "TEXT",
    })
    _migrate_printer(conn)
    # First code is picked at random in the lower half of the space so tags
    # don't start at 00000 and there is room to count upwards for decades.
    if get_setting(conn, "next_num") is None:
        set_setting(conn, "next_num", str(secrets.randbelow(codes.SPACE // 2) + 32 ** 4))
    conn.close()


def _migrate_printer(conn) -> None:
    """Turn the single-printer settings (or PRINTER_HOST) into the first printers row."""
    if conn.execute("SELECT COUNT(*) FROM printers").fetchone()[0]:
        return
    host = get_setting(conn, "printer_host")
    if host is None:
        host = os.environ.get("PRINTER_HOST", "").strip()
    if not host:
        return
    nudge = int(get_setting(conn, "print_nudge_dots") or 0)
    conn.execute("INSERT INTO printers(name, host, nudge_dots, is_default, created_at) VALUES(?,?,?,?,?)",
                 ("PT-P950NW", host, nudge, 1, now()))


def _add_columns(conn, table: str, cols: dict) -> None:
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def get_setting(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def log(conn, code: str, actor: str, action: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO events(code, at, actor, action, detail) VALUES(?,?,?,?,?)",
        (code, now(), actor, action, detail),
    )


def issue_tags(conn, count: int, client_id: int, fmt: str, actor: str, fields: Optional[dict] = None) -> list:
    """Allocate `count` sequential codes atomically and create their rows."""
    fields = fields or {}
    status = "active" if fields.get("serial") or fields.get("device_name") or fields.get("hudu_asset_id") else "unassigned"
    conn.execute("BEGIN IMMEDIATE")
    try:
        start = int(get_setting(conn, "next_num"))
        if start + count > codes.SPACE:
            raise RuntimeError("code space exhausted")
        out = []
        ts = now()
        for i in range(count):
            num = start + i
            code = codes.to_code(num)
            conn.execute(
                "INSERT INTO tags(code, num, client_id, format, status, device_name, model, serial, notes, "
                "hudu_asset_id, hudu_asset_url, created_by, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (code, num, client_id, fmt, status,
                 fields.get("device_name", ""), fields.get("model", ""), fields.get("serial", ""),
                 fields.get("notes", ""), fields.get("hudu_asset_id"), fields.get("hudu_asset_url", ""),
                 actor, ts, ts),
            )
            log(conn, code, actor, "issued", "batch" if count > 1 else "")
            out.append(code)
        set_setting(conn, "next_num", str(start + count))
        conn.execute("COMMIT")
        return out
    except Exception:
        conn.execute("ROLLBACK")
        raise


TAG_SELECT = """
SELECT t.*, c.name AS client_name, c.label_name AS client_label, c.is_internal AS client_internal
FROM tags t
JOIN clients c ON c.id = t.client_id
"""


def get_tag(conn, code: str):
    return conn.execute(TAG_SELECT + " WHERE t.code = ?", (code,)).fetchone()
