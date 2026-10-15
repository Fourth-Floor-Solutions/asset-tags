"""Tag logic shared by several areas: label text, reprint checks, Hudu writes, printing records."""

from __future__ import annotations

from . import db, hudu, labels, log, printer, settings, usage
from .web import PUBLIC_BASE_URL

_log = log.get("hudu")

MAX_BATCH = 100
MAX_PRINT = 200


def label_name(tag, org: dict | None = None) -> str:
    org = org or settings.get_all()
    return org["org_short"] if tag["format"] == "loan" else tag["client_label"]


def label_text(org: dict | None = None) -> dict:
    return settings.label_text(org or settings.get_all())


def printed_as(tag) -> str:
    return f"{tag['format']}|{label_name(tag)}"


def needs_reprint(tag) -> bool:
    return bool(tag["printed_as"]) and tag["printed_as"] != printed_as(tag)


def svg_for(tag, org: dict | None = None) -> str:
    org = org or settings.get_all()
    return labels.render(tag["format"], tag["code"], label_name(tag, org), PUBLIC_BASE_URL, text=label_text(org))


HUDU_OK = ("written", "cleared", "already up to date")


def hudu_needs_sync(tag) -> bool:
    desired = tag["hudu_asset_id"] if tag["status"] != "retired" else None
    status = tag["hudu_sync_status"] or ""
    return desired != tag["hudu_pushed_asset_id"] or (desired is not None and not status.startswith(HUDU_OK))


def hudu_sync(conn, code: str, actor: str, force: bool = False) -> str | None:
    """Keep the linked Hudu asset's Asset Tag field in step with this tag.

    Writes the code to the linked asset, clears it from a previously linked
    asset (only if that asset still holds this code), and clears it when the
    tag is retired. Returns an error message, or None when all went well.
    """
    if not hudu.write_enabled():
        return None
    tag = db.get_tag(conn, code)
    if not tag or not (force or hudu_needs_sync(tag)):
        return None
    desired = tag["hudu_asset_id"] if tag["status"] != "retired" else None
    pushed = tag["hudu_pushed_asset_id"]
    notes, error = [], None
    try:
        if pushed and pushed != desired:
            notes.append(f"asset {pushed}: {hudu.set_tag(pushed, '', only_if=code)}")
            pushed = None
        if desired:
            result = hudu.set_tag(desired, code)
            notes.append(f"asset {desired}: {result}")
            pushed = desired if result in HUDU_OK else None
    except hudu.HuduError as e:
        error = str(e)
        notes.append(f"error: {error}")
        _log.warning("writing tag %s to Hudu failed: %s", code, error)
    status = "; ".join(notes) or "nothing to do"
    if error:
        status = "error: " + error
    elif desired:
        status = notes[-1].split(": ", 1)[1]
    conn.execute(
        "UPDATE tags SET hudu_pushed_asset_id=?, hudu_sync_status=?, hudu_sync_at=? WHERE code=?",
        (pushed, status, db.now(), code),
    )
    db.log(conn, code, actor, "hudu", "; ".join(notes))
    return error


def clients(conn, include_inactive: bool = False):
    sql = "SELECT * FROM clients" + ("" if include_inactive else " WHERE active = 1")
    return conn.execute(sql + " ORDER BY is_internal DESC, name COLLATE NOCASE").fetchall()


def check_format(conn, client_id: int, fmt: str, current_format: str | None = None) -> str | None:
    if fmt not in labels.FORMATS:
        return "Choose a label format."
    if not labels.FORMATS[fmt].get("enabled", True) and fmt != current_format:
        return f"{labels.FORMATS[fmt]['name']} tags are switched off in Settings."
    c = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not c:
        return "Choose a client."
    if fmt == "loan" and not c["is_internal"]:
        own = conn.execute("SELECT name FROM clients WHERE is_internal = 1 LIMIT 1").fetchone()
        return "Loan tags are only for your own kit. " + (
            f"Choose {own['name']} as the client." if own else "Set your organisation's client in Settings first."
        )
    return None


def record_printed(conn, tags: list, actor: str, how: str) -> int:
    for tag in tags:
        conn.execute(
            "UPDATE tags SET printed_count = printed_count + 1, printed_at = ?, printed_as = ? WHERE code = ?",
            (db.now(), printed_as(tag), tag["code"]),
        )
        db.log(conn, tag["code"], actor, "printed", f"{labels.FORMATS[tag['format']]['name']} · {how}")
    return len(tags)


def printers_live() -> list:
    """Every printer with its live status, updating tape tracking as a side effect."""
    with db.connection() as conn:
        rows = printer.all_printers(conn)
        live = printer.with_status(rows)
        for x in live:
            usage.track(conn, x["p"], x["st"])
        return live
