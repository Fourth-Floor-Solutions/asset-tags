"""Retiring tags whose device has been archived in Hudu (issue #21).

The check looks up the Hudu asset of every linked tag, spaced out because
Hudu blocks bursts of requests:

- Asset **archived**, tag in use: the tag is retired, with a history entry
  by "Hudu archive check". The asset's Asset Tag field is left as it is, so
  the archived record still shows which tag the device had.
- Asset **missing** (deleted, or no longer visible to the API key): the tag
  is listed for review and not changed.
- Asset **unarchived** after the check retired its tag: listed for review,
  not restored, since the tag may have been removed or reused since.

It runs every HUDU_ARCHIVE_CHECK_MINUTES (default 60; 0 turns it off) while
Hudu is set up, and an admin can run it from the Clients page.
"""

from __future__ import annotations

import json
import os
import threading
import time

from . import db, hudu, log

ACTOR = "Hudu archive check"
LAST_RUN_KEY = "hudu_archive_last"
REVIEW = ("missing", "unarchived")
_log = log.get("hudu")
_running = threading.Lock()


def interval_minutes() -> int:
    raw = os.environ.get("HUDU_ARCHIVE_CHECK_MINUTES", "60").strip()
    return max(0, int(raw)) if raw.isdigit() else 60


def _set_state(conn, code: str, state: str) -> None:
    conn.execute("UPDATE tags SET hudu_state=?, hudu_state_at=? WHERE code=?", (state, db.now(), code))


def run(conn) -> dict:
    """Check every linked tag once. Returns (and saves) a summary of the run.

    Stops at the first Hudu error other than "not found": those (no network,
    a refused key, rate limiting) would fail for every other tag too.
    """
    if not _running.acquire(blocking=False):
        return {"skipped": "A check is already running."}
    try:
        return _run(conn)
    finally:
        _running.release()


def _run(conn) -> dict:
    started = time.monotonic()
    tags = conn.execute(
        "SELECT code, status, hudu_asset_id, hudu_state FROM tags WHERE hudu_asset_id IS NOT NULL "
        "AND (status != 'retired' OR hudu_state IN ('archived', 'unarchived')) ORDER BY code"
    ).fetchall()
    retired, missing, unarchived, error = [], [], [], ""
    checked = 0
    for n, t in enumerate(tags):
        if n:
            time.sleep(hudu.PAUSE)
        try:
            state = hudu.asset_state(t["hudu_asset_id"])
        except hudu.HuduError as e:
            error = str(e)
            break
        checked += 1
        code = t["code"]
        if t["status"] != "retired":
            if state == "archived":
                conn.execute(
                    "UPDATE tags SET status='retired', updated_at=?, hudu_pushed_asset_id=NULL, "
                    "hudu_sync_status=?, hudu_sync_at=? WHERE code=?",
                    (db.now(), "asset archived in Hudu; its Asset Tag field was left as it is", db.now(), code),
                )
                _set_state(conn, code, "archived")
                db.log(conn, code, ACTOR, "retired", f"Archived in Hudu (asset {t['hudu_asset_id']})")
                retired.append(code)
            elif state == "missing":
                if t["hudu_state"] != "missing":
                    db.log(conn, code, ACTOR, "hudu", f"Hudu asset {t['hudu_asset_id']} not found; check the link")
                _set_state(conn, code, "missing")
                missing.append(code)
            elif t["hudu_state"]:
                _set_state(conn, code, "")
        else:  # retired by an earlier check
            new = {"archived": "archived", "missing": "missing", "active": "unarchived"}[state]
            if new == "unarchived" and t["hudu_state"] != "unarchived":
                db.log(conn, code, ACTOR, "hudu", f"Hudu asset {t['hudu_asset_id']} is no longer archived")
            if new != t["hudu_state"]:
                _set_state(conn, code, new)
            if new == "unarchived":
                unarchived.append(code)
            elif new == "missing":
                missing.append(code)
    summary = {
        "at": db.now(),
        "checked": checked,
        "of": len(tags),
        "retired": retired,
        "missing": missing,
        "unarchived": unarchived,
        "error": error,
        "seconds": round(time.monotonic() - started, 1),
    }
    db.set_setting(conn, LAST_RUN_KEY, json.dumps(summary))
    if error:
        _log.warning("archive check stopped after %d of %d tags: %s", checked, len(tags), error)
    else:
        _log.info(
            "archive check: %d tags checked, %d retired, %d for review",
            checked,
            len(retired),
            len(missing) + len(unarchived),
        )
    return summary


def last_run(conn) -> dict | None:
    raw = db.get_setting(conn, LAST_RUN_KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def for_review(conn) -> list:
    return conn.execute(db.TAG_SELECT + " WHERE t.hudu_state IN ('missing', 'unarchived') ORDER BY t.code").fetchall()


def clear_review(conn, code: str) -> None:
    """A person has dealt with the tag (restored, relinked, unlinked or kept retired)."""
    _set_state(conn, code, "")


# ---------- running on a timer ----------

_stop = threading.Event()
FIRST_RUN_DELAY = 300  # seconds after startup, so a restart doesn't wait an hour or hit Hudu at once


def _loop() -> None:
    wait = FIRST_RUN_DELAY
    while not _stop.wait(wait):
        wait = interval_minutes() * 60 or 3600
        if not interval_minutes() or not hudu.configured():
            continue
        try:
            with db.connection() as conn:
                run(conn)
        except Exception:  # never let the timer thread die
            _log.exception("archive check failed")


def start() -> threading.Thread | None:
    if not interval_minutes():
        _log.info("archive check timer is off (HUDU_ARCHIVE_CHECK_MINUTES=0)")
        return None
    _stop.clear()
    thread = threading.Thread(target=_loop, name="hudu-archive-check", daemon=True)
    thread.start()
    return thread


def stop() -> None:
    _stop.set()
