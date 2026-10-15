"""Printing labels, and managing printers and tape cassettes."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import auth, codes, db, labels, log, printer, settings, usage
from ..tagops import (
    MAX_PRINT,
    label_name,
    label_text,
    printers_live,
    record_printed,
    svg_for,
)
from ..web import PUBLIC_BASE_URL, clean, flash, go, page

router = APIRouter()
_log = log.get("printing")


def _print_codes(raw: str) -> list:
    out = []
    for part in raw.split(","):
        c = codes.parse(part)
        if c and c not in out:
            out.append(c)
    return out[:MAX_PRINT]


@router.get("/print", response_class=HTMLResponse)
def print_sheet(request: Request, batch: str = "", cut: str = ""):
    auth.require_user(request)
    wanted = _print_codes(request.query_params.get("codes", ""))
    with db.connection() as conn:
        tags = [t for t in (db.get_tag(conn, c) for c in wanted) if t]
    org = settings.get_all()
    items = [{"tag": t, "svg": svg_for(t, org), "spec": labels.FORMATS[t["format"]]} for t in tags]
    used = sorted({t["format"] for t in tags})
    pstats = printers_live()
    plan, waiting = printer.route(tags, pstats)
    names = {x["p"]["id"]: x["p"]["name"] for x in pstats}
    target = {t["code"]: names[pid] for pid, ts in plan.items() for t in ts}
    return page(
        request,
        "print.html",
        items=items,
        used_formats=used,
        batch=batch == "1",
        cut_choice=cut if cut in ("cut", "strip") else "",
        pstats=pstats,
        target=target,
        n_ready=len(target),
        code_list=",".join(t["code"] for t in tags),
    )


@router.post("/print/record")
def print_record(request: Request, codes_csv: str = Form(..., alias="codes")):
    user = auth.require_user(request)
    with db.connection() as conn:
        tags = [t for t in (db.get_tag(conn, c) for c in _print_codes(codes_csv)) if t]
        n = record_printed(conn, tags, auth.label(user), "browser")
    return JSONResponse({"recorded": n})


NUDGE_LIMIT = 40  # dots (about 2.8 mm) either way


def _plural(n: int, word: str = "label") -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


@router.post("/print/send")
def print_send(request: Request, codes_csv: str = Form(..., alias="codes"), strip: str = Form("0")):
    """Print straight to the label printers, routing each label to one whose tape fits."""
    user = auth.require_user(request)
    pstats = printers_live()
    if not pstats:
        return JSONResponse(
            {"ok": False, "message": "No label printer is set up. An admin can add one on the Printer page."},
            status_code=502,
        )
    printed, notes, problems = [], [], []
    with db.connection() as conn:
        tags = [t for t in (db.get_tag(conn, c) for c in _print_codes(codes_csv)) if t]
        if not tags:
            return JSONResponse({"ok": False, "message": "Nothing to print."})
        plan, waiting = printer.route(tags, pstats)
        for x in pstats:
            batch = plan.get(x["p"]["id"])
            if not batch:
                continue
            p, st = x["p"], x["st"]
            org = settings.get_all()
            images = [
                labels.raster(t["format"], t["code"], label_name(t, org), PUBLIC_BASE_URL, text=label_text(org))
                for t in batch
            ]
            try:
                printer.send(p["host"], printer.build_job(images, st["tape_mm"], p["nudge_dots"], strip=strip == "1"))
            except printer.PrinterError as e:
                _log.warning("print job to %s (%s) failed: %s", p["name"], p["host"], e)
                problems.append(f"{p['name']}: {e}")
                waiting += batch
                continue
            problem = printer.wait_done(p["host"])
            if problem:
                _log.warning("%s reported a problem after printing: %s", p["name"], problem)
                problems.append(f"{p['name']}: {problem}")
            record_printed(conn, batch, auth.label(user), p["name"])
            usage.track(conn, p, st)
            left = usage.summary(conn, p["id"], st)
            if left and left["low"]:
                problems.append(f"Tape is low in {p['name']}: about {left['left_cm'] / 100:.1f} m left.")
            printed += batch
            notes.append(
                f"{_plural(len(batch))} on {p['name']} ({st['tape_mm']} mm {st['tape_colour']})"
                + (" as one strip with cut lines" if strip == "1" and len(batch) > 1 else "")
            )
    if printed:
        msg = "Printed " + "; ".join(notes) + "."
    else:
        offline = [f"{x['p']['name']}: {x['err']}" for x in pstats if x["err"]]
        msg = "Nothing printed. " + (" ".join(offline) if offline and len(offline) == len(pstats) else "")
    if waiting:
        need = sorted({labels.FORMATS[t["format"]]["tape"] for t in waiting})
        msg += f" {_plural(len(waiting))} still to print: load {', '.join(need)} in a printer and press Print again."
    if problems:
        msg += " " + " ".join(problems)
    return JSONResponse(
        {
            "ok": bool(printed) and not problems,
            "message": msg.strip(),
            "printed": [t["code"] for t in printed],
            "waiting": [t["code"] for t in waiting],
        }
    )


@router.get("/printer", response_class=HTMLResponse)
def printer_page(request: Request):
    auth.require_user(request)
    pstats = printers_live()
    with db.connection() as conn:
        for x in pstats:
            x["cassette"] = usage.summary(conn, x["p"]["id"], x["st"])
    for x in pstats:
        x["fits"] = [k for k in labels.enabled() if x["st"] and not printer.check_tape(x["st"], k)]
        x["nudge_mm"] = round(abs(x["p"]["nudge_dots"]) / labels.DOTS_PER_MM, 2)
    return page(request, "printer.html", pstats=pstats)


def _printer_form(name: str, address: str):
    host = printer.clean_host(address)
    name = clean(name, 60)
    if not host:
        return None, None, "Enter the printer's IP address or host name, for example 192.168.1.50."
    if not name:
        return None, None, 'Give the printer a name, for example "Office label printer".'
    return name, host, None


def _check_message(name: str, host: str):
    try:
        st = printer.status(host)
        return f"{name} saved. Found a {st['model']} at {host} with {st['tape_mm'] or 'no'} mm tape loaded.", "ok"
    except printer.PrinterError as e:
        return f"{name} saved, but nothing answered at {host} yet ({e}).", "warn"


@router.post("/cassettes/{cid}/new")
def cassette_new(request: Request, cid: int):
    auth.require_user(request)
    with db.connection() as conn:
        conn.execute(
            "UPDATE cassettes SET used_cm = 0, estimated = 0, low_logged = 0, fitted_at = ? WHERE id = ?",
            (db.now(), cid),
        )
    flash(request, "New cassette recorded. Tape use starts again from zero.")
    return go("/printer")


@router.post("/cassettes/{cid}")
def cassette_set(request: Request, cid: int, used_m: str = Form(""), length_m: str = Form("")):
    auth.require_user(request)
    try:
        used = round(float(used_m or 0) * 100)
        length = round(float(length_m or 8) * 100)
    except ValueError:
        flash(request, "Enter metres as a number, for example 2.5.", "bad")
        return go("/printer")
    if not (0 <= used <= 10000 and 100 <= length <= 10000):
        flash(request, "That doesn't look right: used must be 0–100 m and the cassette length 1–100 m.", "bad")
        return go("/printer")
    with db.connection() as conn:
        conn.execute("UPDATE cassettes SET used_cm = ?, length_cm = ? WHERE id = ?", (used, length, cid))
    flash(request, "Cassette updated.")
    return go("/printer")


@router.post("/printers/new")
def printer_new(request: Request, name: str = Form(""), address: str = Form("")):
    auth.require_admin(request)
    name, host, err = _printer_form(name, address)
    if err:
        flash(request, err, "bad")
        return go("/printer")
    with db.connection() as conn:
        first = conn.execute("SELECT COUNT(*) FROM printers").fetchone()[0] == 0
        conn.execute(
            "INSERT INTO printers(name, host, nudge_dots, is_default, created_at) VALUES(?,?,0,?,?)",
            (name, host, 1 if first else 0, db.now()),
        )
    flash(request, *_check_message(name, host))
    return go("/printer")


def _get_printer(conn, pid: int):
    return conn.execute("SELECT * FROM printers WHERE id = ?", (pid,)).fetchone()


@router.post("/printers/{pid}")
def printer_update(
    request: Request, pid: int, name: str = Form(""), address: str = Form(""), dots: int = Form(0), then: str = Form("")
):
    auth.require_admin(request)
    name, host, err = _printer_form(name, address)
    if err:
        flash(request, err, "bad")
        return go("/printer")
    dots = max(-NUDGE_LIMIT, min(NUDGE_LIMIT, dots))
    with db.connection() as conn:
        old = _get_printer(conn, pid)
        if not old:
            return go("/printer")
        conn.execute("UPDATE printers SET name=?, host=?, nudge_dots=? WHERE id=?", (name, host, dots, pid))
    if then == "test":
        return printer_test(request, pid)
    if host != old["host"]:
        flash(request, *_check_message(name, host))
    else:
        flash(request, f"{name} saved.")
    return go("/printer")


@router.post("/printers/{pid}/default")
def printer_default(request: Request, pid: int):
    auth.require_admin(request)
    with db.connection() as conn:
        if _get_printer(conn, pid):
            conn.execute("UPDATE printers SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END", (pid,))
            flash(request, "Default printer changed. It's tried first for every print.")
    return go("/printer")


@router.post("/printers/{pid}/delete")
def printer_delete(request: Request, pid: int):
    auth.require_admin(request)
    with db.connection() as conn:
        p = _get_printer(conn, pid)
        if p:
            conn.execute("DELETE FROM printers WHERE id = ?", (pid,))
            if p["is_default"]:
                conn.execute("UPDATE printers SET is_default = 1 WHERE id = (SELECT MIN(id) FROM printers)")
            flash(request, f"Removed {p['name']}.")
    return go("/printer")


@router.post("/printers/{pid}/test")
def printer_test(request: Request, pid: int):
    auth.require_user(request)
    with db.connection() as conn:
        p = _get_printer(conn, pid)
    if not p:
        return go("/printer")
    try:
        st = printer.status(p["host"])
    except printer.PrinterError as e:
        flash(request, str(e), "bad")
        return go("/printer")
    fmt = next(
        (
            k
            for k in ("slim", "standard", "micro", "loan")
            if labels.FORMATS[k].get("enabled", True) and not printer.check_tape(st, k)
        ),
        None,
    )
    if not fmt:
        flash(
            request,
            f"None of the tag formats fit the tape in {p['name']} ({st['tape_mm']} mm {st['tape_colour']}).",
            "bad",
        )
        return go("/printer")
    image = labels.raster(fmt, "TEST0", "Test print", PUBLIC_BASE_URL, text=label_text())
    try:
        printer.send(p["host"], printer.build_job([image], st["tape_mm"], p["nudge_dots"]))
    except printer.PrinterError as e:
        flash(request, str(e), "bad")
        return go("/printer")
    problem = printer.wait_done(p["host"])
    flash(
        request,
        problem or f"Test label sent to {p['name']} ({labels.FORMATS[fmt]['name']} format).",
        "bad" if problem else "ok",
    )
    return go("/printer")
