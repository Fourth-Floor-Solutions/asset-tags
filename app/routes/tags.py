"""The Tags list, issuing tags, one tag's page, and Hudu writes and lookups."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import auth, codes, db, hudu, labels
from ..tagops import (
    MAX_BATCH,
    check_format,
    clients,
    hudu_needs_sync,
    hudu_sync,
    needs_reprint,
    svg_for,
)
from ..web import PUBLIC_BASE_URL, clean, flash, go, hudu_link, page

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, q: str = "", client_id: str = "", status: str = ""):
    auth.require_user(request)
    q = q.strip()
    code = codes.parse(q) if q else None
    if code:
        with db.connection() as conn:
            if db.get_tag(conn, code):
                return go(f"/tags/{code}")
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
    sql = db.TAG_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY t.updated_at DESC LIMIT 300"
    with db.connection() as conn:
        rows = conn.execute(sql, args).fetchall()
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM tags GROUP BY status")}
        reprints = [
            r
            for r in conn.execute(db.TAG_SELECT + " WHERE t.printed_as IS NOT NULL AND t.status != 'retired'")
            if needs_reprint(r)
        ]
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


@router.get("/tags/new", response_class=HTMLResponse)
def tag_new_form(request: Request, client_id: str = ""):
    auth.require_user(request)
    with db.connection() as conn:
        cl = clients(conn)
    return page(request, "tag_new.html", clients=cl, sel_client=client_id, form={})


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
    cut: str = Form("strip"),
):
    user = auth.require_user(request)
    with db.connection() as conn:
        err = check_format(conn, client_id, format)
        if not err and not 1 <= count <= MAX_BATCH:
            err = f"Issue between 1 and {MAX_BATCH} tags at a time."
        if err:
            return page(request, "batch.html", status_code=400, clients=clients(conn), error=err)
        issued = db.issue_tags(conn, count, client_id, format, auth.label(user))
    return go("/print?codes=" + ",".join(issued) + "&batch=1&cut=" + ("cut" if cut == "cut" else "strip"))


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
            herr = hudu_sync(conn, code, auth.label(user))
            if herr:
                flash(request, f"Tag {action}, but Hudu wasn't updated: {herr}", "warn")
    return go(f"/tags/{code}")


@router.post("/tags/{code}/hudu")
def tag_hudu(request: Request, code: str):
    user = auth.require_user(request)
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
    with db.connection() as conn:
        c = conn.execute("SELECT hudu_id FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not c or not c["hudu_id"]:
        return JSONResponse({"error": "This client isn't linked to a Hudu company."}, status_code=400)
    try:
        return {"assets": hudu.search_assets(c["hudu_id"], q.strip()[:80])}
    except hudu.HuduError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
