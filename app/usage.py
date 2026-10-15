"""Tape used per cassette, from the printer's lifetime print-length counter.

Each time the app talks to a printer it reads the counter and adds the change
since the last read to the cassette that was loaded then. Cassettes are told
apart by width and colour, so swapping between them keeps both totals.
"""

from __future__ import annotations

from . import db, labels, printer

LOW_CM = 100  # warn when about 1 m is left
LEAD_IN_MM = 25  # blank the printer cuts off at the start of every job


def tape_key(st: dict | None) -> str | None:
    return f"{st['tape_mm']}|{st['tape_colour']}" if st and st.get("tape_mm") else None


def _cassette(conn, printer_id: int, key: str):
    row = conn.execute("SELECT * FROM cassettes WHERE printer_id = ? AND tape = ?", (printer_id, key)).fetchone()
    if not row:
        conn.execute("INSERT INTO cassettes(printer_id, tape, fitted_at) VALUES(?,?,?)", (printer_id, key, db.now()))
        row = conn.execute("SELECT * FROM cassettes WHERE printer_id = ? AND tape = ?", (printer_id, key)).fetchone()
    return row


def track(conn, p, st: dict | None) -> None:
    """Read the counter and charge any tape printed since last time."""
    if not st:
        return
    count = printer.counter_cm(p["host"])
    if count is None:
        return
    row = conn.execute("SELECT last_counter_cm, last_tape FROM printers WHERE id = ?", (p["id"],)).fetchone()
    last, last_tape = row["last_counter_cm"], row["last_tape"]
    if last is not None and last_tape and count > last:
        _cassette(conn, p["id"], last_tape)
        conn.execute(
            "UPDATE cassettes SET used_cm = used_cm + ? WHERE printer_id = ? AND tape = ?",
            (count - last, p["id"], last_tape),
        )
    key = tape_key(st)
    if key:
        _cassette(conn, p["id"], key)
    conn.execute("UPDATE printers SET last_counter_cm = ?, last_tape = ? WHERE id = ?", (count, key, p["id"]))


def summary(conn, printer_id: int, st: dict | None) -> dict | None:
    key = tape_key(st)
    if not key or st is None:
        return None
    c = _cassette(conn, printer_id, key)
    left = max(0, c["length_cm"] - c["used_cm"])
    fmts = [k for k in labels.enabled() if not printer.check_tape(st, k)]
    per = None
    if fmts:
        per = labels.FORMATS[fmts[0]]["width"] / 10 + 0.2  # cm per label in a batch
    return {
        "id": c["id"],
        "tape": key,
        "used_cm": c["used_cm"],
        "length_cm": c["length_cm"],
        "left_cm": left,
        "pct": min(100, round(100 * c["used_cm"] / c["length_cm"])) if c["length_cm"] else 0,
        "estimated": bool(c["estimated"]),
        "fitted_at": c["fitted_at"],
        "low": left <= LOW_CM,
        "fmt_name": labels.FORMATS[fmts[0]]["name"] if fmts else None,
        "labels_batch": int(left // per) if per else None,
        "labels_single": int(left // (per + LEAD_IN_MM / 10)) if per else None,
    }
