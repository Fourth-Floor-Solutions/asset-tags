"""The Tags list, issuing tags, one tag's page, and Hudu writes and lookups."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import auth, codes, csvout, db, hudu, hudu_archive, labels, log, printer
from ..tagops import (
    LABEL_PROBLEMS,
    MAX_BATCH,
    check_format,
    clients,
    hudu_needs_sync,
    hudu_sync,
    needs_reprint,
    reprint_queue,
    reprint_reasons,
    svg_for,
)
from ..web import PUBLIC_BASE_URL, clean, flash, go, hudu_link, page

router = APIRouter()


def _tag_filter(q: str, client_id: str, status: str) -> tuple[str, list]:
    """The Tags list's search and filters as a WHERE clause (shared with the CSV export)."""
    where: list[str] = []
    args: list[object] = []
    if q:
        like = f"%{q}%"
        where.append(
            "(t.code LIKE ? OR t.serial LIKE ? OR t.device_name LIKE ? OR t.model LIKE ? "
            "OR c.name LIKE ? OR c.label_name LIKE ? OR t.notes LIKE ?)"
        )
        args += [f"%{codes.normalise(q)}%", like, like, like, like, like, like]
    if client_id.isdigit():
        where.append("t.client_id = ?")
        args.append(int(client_id))
    if status in ("unassigned", "active", "retired"):
        where.append("t.status = ?")
        args.append(status)
    return (" WHERE " + " AND ".join(where) if where else ""), args


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, q: str = "", client_id: str = "", status: str = ""):
    auth.require_user(request)
    q = q.strip()
    code = codes.parse(q) if q else None
    if code:
        with db.connection() as conn:
            if db.get_tag(conn, code):
                return go(f"/tags/{code}")
    where_sql, args = _tag_filter(q, client_id, status)
    sql = db.TAG_SELECT + where_sql + " ORDER BY t.updated_at DESC LIMIT 300"
    with db.connection() as conn:
        rows = conn.execute(sql, args).fetchall()
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM tags GROUP BY status")}
        reprints = reprint_queue(conn)
        cl = clients(conn)
    return page(
        request,
        "dashboard.html",
        tags=rows,
        q=q,
        client_id=client_id,
        status=status,
        counts=counts,
        reprint_count=len(reprints),
        clients=cl,
        needs_reprint=needs_reprint,
    )


EXPORT_STATUS = {"active": "Assigned", "unassigned": "Not assigned", "retired": "Retired"}


@router.get("/tags.csv")
def tags_csv(request: Request, q: str = "", client_id: str = "", status: str = ""):
    """The Tags list as CSV, with the same search and filters, and no row limit."""
    user = auth.require_user(request)
    where_sql, args = _tag_filter(q.strip(), client_id, status)
    with db.connection() as conn:
        rows = conn.execute(db.TAG_SELECT + where_sql + " ORDER BY c.name COLLATE NOCASE, t.code", args).fetchall()
        name = "tags"
        if client_id.isdigit():
            c = conn.execute("SELECT name FROM clients WHERE id = ?", (int(client_id),)).fetchone()
            name = f"tags-{csvout.safe_name(c['name'])}" if c else name
    log.get("tags").info("%s exported %d tags as CSV", auth.label(user), len(rows))
    return csvout.response(
        [
            "Code",
            "Status",
            "Format",
            "Client",
            "Device",
            "Model",
            "Serial",
            "Hudu asset",
            "Hudu link",
            "Printed",
            "Last printed",
            "Created",
            "Updated",
            "Notes",
        ],
        (
            [
                t["code"],
                EXPORT_STATUS.get(t["status"], t["status"]),
                labels.FORMATS.get(t["format"], {}).get("name", t["format"]),
                t["client_name"],
                t["device_name"],
                t["model"],
                t["serial"],
                t["hudu_asset_id"],
                t["hudu_asset_url"],
                t["printed_count"],
                t["printed_at"],
                t["created_at"],
                t["updated_at"],
                t["notes"],
            ]
            for t in rows
        ),
        f"{name}-{db.now()[:10]}.csv",
    )


@router.get("/tags/new", response_class=HTMLResponse)
def tag_new_form(request: Request, client_id: str = ""):
    auth.require_user(request)
    with db.connection() as conn:
        cl = clients(conn)
    # Prefilled from coverage's "Tag this device"; the Hudu link only while Hudu is set up.
    q = request.query_params
    form = {k: clean(q.get(k), 120) for k in ("device_name", "model", "serial") if q.get(k)}
    if hudu.configured() and (q.get("hudu_asset_id") or "").isdigit():
        form["hudu_asset_id"] = q["hudu_asset_id"]
        form["hudu_asset_url"] = hudu_link(q.get("hudu_asset_url") or "")
    return page(request, "tag_new.html", clients=cl, sel_client=client_id, form=form)


@router.post("/tags/new")
def tag_new(
    request: Request,
    client_id: int = Form(...),
    format: str = Form("standard"),
    device_name: str = Form(""),
    model: str = Form(""),
    serial: str = Form(""),
    notes: str = Form(""),
    hudu_asset_id: str = Form(""),
    hudu_asset_url: str = Form(""),
):
    user = auth.require_user(request)
    fields = {
        "device_name": clean(device_name, 120),
        "model": clean(model, 120),
        "serial": clean(serial, 120),
        "notes": clean(notes, 2000),
        "hudu_asset_id": int(hudu_asset_id) if hudu_asset_id.isdigit() else None,
        "hudu_asset_url": hudu_link(hudu_asset_url) if hudu_asset_id.isdigit() else "",
    }
    with db.connection() as conn:
        err = check_format(conn, client_id, format)
        if not err and fields["serial"]:
            dup = conn.execute(
                "SELECT code FROM tags WHERE serial = ? AND status != 'retired'", (fields["serial"],)
            ).fetchone()
            if dup:
                err = f"Serial {fields['serial']} is already tagged as {dup['code']}. Open that tag instead."
        if err:
            return page(
                request,
                "tag_new.html",
                status_code=400,
                clients=clients(conn),
                sel_client=str(client_id),
                form=dict(fields, format=format),
                error=err,
            )
        code = db.issue_tags(conn, 1, client_id, format, auth.label(user), fields)[0]
        herr = hudu_sync(conn, code, auth.label(user))
    if herr:
        flash(request, f"Tag {code} issued, but it couldn't be written to Hudu: {herr}", "warn")
    else:
        flash(request, f"Tag {code} issued. Print it and stick it on the device.")
    return go(f"/tags/{code}")


@router.get("/tags/batch", response_class=HTMLResponse)
def batch_form(request: Request):
    auth.require_user(request)
    with db.connection() as conn:
        cl = clients(conn)
    return page(request, "batch.html", clients=cl)


@router.post("/tags/batch")
def batch(
    request: Request,
    client_id: int = Form(...),
    format: str = Form("standard"),
    count: int = Form(...),
    cut: str = Form("precut"),
):
    user = auth.require_user(request)
    with db.connection() as conn:
        err = check_format(conn, client_id, format)
        if not err and not 1 <= count <= MAX_BATCH:
            err = f"Issue between 1 and {MAX_BATCH} tags at a time."
        if err:
            return page(request, "batch.html", status_code=400, clients=clients(conn), error=err)
        issued = db.issue_tags(conn, count, client_id, format, auth.label(user))
    return go("/print?codes=" + ",".join(issued) + "&batch=1&cut=" + (cut if cut in printer.CUT_MODES else "precut"))


@router.get("/tags/{code}", response_class=HTMLResponse)
def tag_detail(request: Request, code: str):
    auth.require_user(request)
    canon = codes.parse(code)
    if not canon:
        return page(
            request,
            "message.html",
            status_code=404,
            heading="Not a tag code",
            body="Tag codes are five characters, like 7KQ3M.",
        )
    if canon != code:
        return go(f"/tags/{canon}")
    with db.connection() as conn:
        tag = db.get_tag(conn, canon)
        if not tag:
            return page(
                request,
                "message.html",
                status_code=404,
                heading=f"{canon} hasn't been issued",
                body="No tag with this code exists. Check the code on the label, or issue a new tag.",
            )
        events = conn.execute("SELECT * FROM events WHERE code = ? ORDER BY id DESC", (canon,)).fetchall()
        cl = clients(conn, include_inactive=True)
    return page(
        request,
        "tag.html",
        tag=tag,
        svg=svg_for(tag),
        events=events,
        clients=cl,
        hudu_write=hudu.write_enabled(),
        hudu_field=hudu.tag_label(),
        reprint=needs_reprint(tag),
        label_problems=LABEL_PROBLEMS,
        qr_url=labels.qr_data(PUBLIC_BASE_URL, canon),
    )


@router.post("/tags/{code}")
def tag_update(
    request: Request,
    code: str,
    client_id: int = Form(...),
    format: str = Form(...),
    device_name: str = Form(""),
    model: str = Form(""),
    serial: str = Form(""),
    notes: str = Form(""),
    hudu_asset_id: str = Form(""),
    hudu_asset_url: str = Form(""),
):
    user = auth.require_user(request)
    with db.connection() as conn:
        tag = db.get_tag(conn, code)
        if not tag:
            return go("/")
        err = check_format(conn, client_id, format, current_format=tag["format"])
        if err:
            flash(request, err, "bad")
            return go(f"/tags/{code}")
        new = {
            "client_id": client_id,
            "format": format,
            "device_name": clean(device_name, 120),
            "model": clean(model, 120),
            "serial": clean(serial, 120),
            "notes": clean(notes, 2000),
            "hudu_asset_id": int(hudu_asset_id) if hudu_asset_id.isdigit() else None,
            "hudu_asset_url": hudu_link(hudu_asset_url) if hudu_asset_id.isdigit() else "",
        }
        if not hudu.configured():
            # The asset picker is hidden while Hudu is off: keep the tag's existing link.
            new["hudu_asset_id"], new["hudu_asset_url"] = tag["hudu_asset_id"], tag["hudu_asset_url"]
        changes = []
        for k, v in new.items():
            if (tag[k] or None) != (v or None):
                if k == "client_id":
                    old_c = tag["client_name"]
                    new_c = conn.execute("SELECT name FROM clients WHERE id = ?", (v,)).fetchone()["name"]
                    changes.append(f"client: {old_c} → {new_c}")
                elif k in ("notes", "hudu_asset_url"):
                    changes.append(k.replace("_", " "))
                else:
                    changes.append(f"{k.replace('_', ' ')}: {tag[k] or '—'} → {v or '—'}")
        if not changes:
            return go(f"/tags/{code}")
        status = tag["status"]
        if status == "unassigned" and (new["serial"] or new["device_name"] or new["hudu_asset_id"]):
            status = "active"
        conn.execute(
            "UPDATE tags SET client_id=?, format=?, device_name=?, model=?, serial=?, notes=?, hudu_asset_id=?, "
            "hudu_asset_url=?, status=?, updated_at=? WHERE code=?",
            (
                new["client_id"],
                new["format"],
                new["device_name"],
                new["model"],
                new["serial"],
                new["notes"],
                new["hudu_asset_id"],
                new["hudu_asset_url"],
                status,
                db.now(),
                code,
            ),
        )
        db.log(conn, code, auth.label(user), "edited", "; ".join(changes))
        if new["hudu_asset_id"] != tag["hudu_asset_id"]:
            hudu_archive.clear_review(conn, code)  # relinked or unlinked: the old asset no longer matters
        herr = hudu_sync(conn, code, auth.label(user))
        updated = db.get_tag(conn, code)
    if herr:
        flash(request, f"Saved, but Hudu wasn't updated: {herr}", "warn")
    elif needs_reprint(updated):
        flash(request, "Saved. The printed label no longer matches, so print a replacement.", "warn")
    else:
        flash(request, "Saved.")
    return go(f"/tags/{code}")


@router.post("/tags/{code}/status")
def tag_status(request: Request, code: str, status: str = Form(...), reason: str = Form("")):
    user = auth.require_user(request)
    if status not in ("retired", "active", "unassigned"):
        return go(f"/tags/{code}")
    with db.connection() as conn:
        tag = db.get_tag(conn, code)
        if tag and tag["status"] != status:
            conn.execute("UPDATE tags SET status=?, updated_at=? WHERE code=?", (status, db.now(), code))
            action = "retired" if status == "retired" else "restored"
            db.log(conn, code, auth.label(user), action, clean(reason, 300))
            hudu_archive.clear_review(conn, code)
            herr = hudu_sync(conn, code, auth.label(user))
            if herr:
                flash(request, f"Tag {action}, but Hudu wasn't updated: {herr}", "warn")
    return go(f"/tags/{code}")


@router.post("/tags/{code}/label-problem")
def tag_label_problem(request: Request, code: str, problem: str = Form(""), note: str = Form("")):
    """Report that a tag's label needs replacing (puts it on the Reprint list), or clear the report."""
    user = auth.require_user(request)
    with db.connection() as conn:
        tag = db.get_tag(conn, code)
        if not tag or tag["status"] == "retired":
            return go(f"/tags/{code}")
        if problem in LABEL_PROBLEMS:
            conn.execute(
                "UPDATE tags SET label_problem=?, label_problem_note=?, updated_at=? WHERE code=?",
                (problem, clean(note, 300), db.now(), code),
            )
            db.log(
                conn,
                code,
                auth.label(user),
                "label reported",
                LABEL_PROBLEMS[problem] + (f": {clean(note, 300)}" if note.strip() else ""),
            )
            flash(request, "Added to the Reprint list.")
        elif problem == "" and tag["label_problem"]:
            conn.execute("UPDATE tags SET label_problem='', label_problem_note='' WHERE code=?", (code,))
            db.log(conn, code, auth.label(user), "label report cleared")
    return go(f"/tags/{code}")


@router.get("/reprint", response_class=HTMLResponse)
def reprint_page(request: Request):
    auth.require_user(request)
    with db.connection() as conn:
        rows = reprint_queue(conn)
    return page(
        request,
        "reprint.html",
        tags=rows,
        reasons={t["code"]: reprint_reasons(t) for t in rows},
        code_list=",".join(t["code"] for t in rows),
    )


@router.post("/tags/{code}/keep-retired")
def tag_keep_retired(request: Request, code: str):
    """The Hudu asset was unarchived, but the tag should stay retired."""
    user = auth.require_user(request)
    hudu.require()
    with db.connection() as conn:
        tag = db.get_tag(conn, code)
        if tag and tag["status"] == "retired" and tag["hudu_state"] == "unarchived":
            hudu_archive.clear_review(conn, code)
            db.log(conn, code, auth.label(user), "hudu", "Kept retired after the Hudu asset was unarchived")
    return go(f"/tags/{code}")


@router.post("/hudu/archive-check")
def hudu_archive_check(request: Request):
    auth.require_admin(request)
    hudu.require()
    with db.connection() as conn:
        s = hudu_archive.run(conn)
    if s.get("skipped"):
        flash(request, s["skipped"], "warn")
    elif s["error"]:
        flash(request, f"The check stopped after {s['checked']} of {s['of']} tags: {s['error']}", "bad")
    else:
        review = len(s["missing"]) + len(s["unarchived"])
        flash(
            request,
            f"Checked {s['checked']} tags in Hudu: {len(s['retired'])} retired"
            + (f", {review} to review below." if review else "."),
            "warn" if review else "ok",
        )
    return go("/clients")


@router.post("/tags/{code}/hudu")
def tag_hudu(request: Request, code: str):
    user = auth.require_user(request)
    hudu.require()
    with db.connection() as conn:
        err = hudu_sync(conn, code, auth.label(user), force=True)
        tag = db.get_tag(conn, code)
    if err:
        flash(request, f"Hudu wasn't updated: {err}", "bad")
    elif tag:
        flash(request, f"Hudu: {tag['hudu_sync_status']}.")
    return go(f"/tags/{code}")


@router.post("/hudu/sync-all")
def hudu_sync_all(request: Request):
    user = auth.require_admin(request)
    hudu.require()
    if not hudu.write_enabled():
        flash(request, "Writing to Hudu is switched off (HUDU_WRITE isn't 1).", "bad")
        return go("/clients")
    done = failed = 0
    with db.connection() as conn:
        for t in conn.execute(db.TAG_SELECT).fetchall():
            if not hudu_needs_sync(t):
                continue
            if hudu_sync(conn, t["code"], auth.label(user)):
                failed += 1
            else:
                done += 1
    flash(
        request,
        f"Hudu updated for {done} tag(s)" + (f"; {failed} failed, open them to see why." if failed else "."),
        "warn" if failed else "ok",
    )
    return go("/clients")


@router.get("/api/hudu/assets")
def hudu_assets(request: Request, client_id: int, q: str = ""):
    auth.require_user(request)
    hudu.require()
    with db.connection() as conn:
        c = conn.execute("SELECT hudu_id FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not c or not c["hudu_id"]:
        return JSONResponse({"error": "This client isn't linked to a Hudu company."}, status_code=400)
    try:
        return {"assets": hudu.search_assets(c["hudu_id"], q.strip()[:80])}
    except hudu.HuduError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
