"""Tag a site from a phone (issue #34): scan a tag, link it to the device, save, scan the next.

The page (tag_a_site.html + static/site.js) does the scanning in the browser
with jsQR. These JSON endpoints use the signed-in session like the rest of
the app; posts are covered by the same-site check in main.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import auth, codes, coverage, db, hudu, hudu_archive
from ..tagops import clients, hudu_sync, mark_seen
from ..web import clean, hudu_link, page

router = APIRouter()


def _tag_info(t) -> dict:
    return {
        "code": t["code"],
        "status": t["status"],
        "client_id": t["client_id"],
        "client_name": t["client_name"],
        "device_name": t["device_name"] or "",
        "model": t["model"] or "",
        "serial": t["serial"] or "",
        "hudu_asset_id": t["hudu_asset_id"],
        "printed": bool(t["printed_count"]),
    }


@router.get("/tag-a-site", response_class=HTMLResponse)
def tag_a_site(request: Request):
    auth.require_user(request)
    with db.connection() as conn:
        cl = clients(conn)
    return page(request, "tag_a_site.html", clients=cl, mode="tag")


@router.get("/stocktake", response_class=HTMLResponse)
def stocktake(request: Request):
    auth.require_user(request)
    with db.connection() as conn:
        cl = clients(conn)
    return page(request, "tag_a_site.html", clients=cl, mode="stocktake")


@router.post("/api/site/seen")
async def site_seen(request: Request):
    """Stocktake: record a scanned tag as seen. Answers with what it is, so the phone can show it."""
    user = auth.require_user(request)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Send JSON."}, status_code=400)
    canon = codes.parse(str((body or {}).get("code") or "")) if isinstance(body, dict) else None
    with db.connection() as conn:
        t = db.get_tag(conn, canon) if canon else None
        if not t:
            return JSONResponse({"error": "That isn't a TagDesk tag here."}, status_code=404)
        mark_seen(conn, t["code"], auth.label(user), clean(str(body.get("site") or ""), 80))
        info = _tag_info(db.get_tag(conn, t["code"]))
    info["other_client"] = str(t["client_id"]) != str(body.get("client_id"))
    return info


@router.get("/api/site/unseen")
def site_unseen(request: Request, client_id: int, since: str):
    """Stocktake finish: the client's tags in use that weren't seen since the stocktake started."""
    auth.require_user(request)
    with db.connection() as conn:
        rows = conn.execute(
            db.TAG_SELECT + " WHERE t.client_id = ? AND t.status = 'active' "
            "AND (t.last_seen_at IS NULL OR t.last_seen_at < ?) ORDER BY t.device_name COLLATE NOCASE, t.code",
            (client_id, since[:25]),
        ).fetchall()
    return {"tags": [_tag_info(t) | {"last_seen_at": t["last_seen_at"]} for t in rows]}


@router.get("/not-seen", response_class=HTMLResponse)
def not_seen(request: Request, client_id: str = "", months: int = 12):
    """Tags in use that nobody has scanned for a while: candidates to find, or retire."""
    from datetime import UTC, datetime, timedelta

    auth.require_user(request)
    months = months if months in (3, 6, 12, 24) else 12
    cutoff = (datetime.now(UTC) - timedelta(days=30 * months)).replace(microsecond=0).isoformat()
    where = "t.status = 'active' AND COALESCE(t.last_seen_at, t.created_at) < ?"
    args: list[object] = [cutoff]
    if client_id.isdigit():
        where += " AND t.client_id = ?"
        args.append(int(client_id))
    with db.connection() as conn:
        rows = conn.execute(
            db.TAG_SELECT + " WHERE " + where + " ORDER BY c.name COLLATE NOCASE, t.last_seen_at", args
        ).fetchall()
        cl = clients(conn)
    return page(request, "not_seen.html", tags=rows, clients=cl, client_id=client_id, months=months)


@router.get("/api/site/tag/{code}")
def site_tag(request: Request, code: str):
    auth.require_user(request)
    canon = codes.parse(code)
    with db.connection() as conn:
        t = db.get_tag(conn, canon) if canon else None
    if not t:
        return JSONResponse({"error": f"{code} isn't a TagDesk tag here."}, status_code=404)
    return _tag_info(t)


@router.get("/api/site/devices")
def site_devices(request: Request, client_id: int):
    """The client's Hudu devices without a tag, from the coverage cache (no Hudu call)."""
    auth.require_user(request)
    if not hudu.configured():
        return {"devices": [], "fetched": None}
    with db.connection() as conn:
        c = conn.execute("SELECT * FROM clients WHERE id = ? AND hudu_id IS NOT NULL", (client_id,)).fetchone()
        if not c:
            return {"devices": [], "fetched": None}
        cov = coverage.for_client(conn, c)
    return {"devices": cov["untagged"][:500], "fetched": cov["refreshed_at"]}


@router.post("/api/site/assign")
async def site_assign(request: Request):
    user = auth.require_user(request)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Send JSON."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Send a JSON object."}, status_code=400)
    canon = codes.parse(str(body.get("code") or ""))
    site = clean(str(body.get("site") or ""), 80)
    with db.connection() as conn:
        t = db.get_tag(conn, canon) if canon else None
        if not t:
            return JSONResponse({"error": "That isn't a TagDesk tag here."}, status_code=404)
        if t["status"] == "retired":
            return JSONResponse({"error": f"{t['code']} is retired. Use a new tag."}, status_code=409)
        if str(t["client_id"]) != str(body.get("client_id")):
            return JSONResponse(
                {"error": f"{t['code']} is printed for {t['client_name']}, not this client. Use one of their tags."},
                status_code=409,
            )
        if t["status"] == "active" and not body.get("replace"):
            return JSONResponse(
                {"error": f"{t['code']} is already on {t['device_name'] or 'a device'}.", "tag": _tag_info(t)},
                status_code=409,
            )
        new: dict[str, object] = {
            "device_name": clean(str(body.get("device_name") or ""), 120),
            "model": clean(str(body.get("model") or ""), 120),
            "serial": clean(str(body.get("serial") or ""), 120),
            "hudu_asset_id": None,
            "hudu_asset_url": "",
        }
        hid = str(body.get("hudu_asset_id") or "")
        if hid.isdigit() and hudu.configured():
            new["hudu_asset_id"] = int(hid)
            new["hudu_asset_url"] = hudu_link(str(body.get("hudu_asset_url") or ""))
        if not (new["device_name"] or new["serial"] or new["hudu_asset_id"]):
            return JSONResponse({"error": "Add a device name, a serial number or a Hudu device."}, status_code=400)
        conn.execute(
            "UPDATE tags SET device_name=?, model=?, serial=?, hudu_asset_id=?, hudu_asset_url=?, status='active', "
            "updated_at=? WHERE code=?",
            (*new.values(), db.now(), t["code"]),
        )
        detail = "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in new.items() if v and k != "hudu_asset_url")
        db.log(conn, t["code"], auth.label(user), "assigned on site", (f"{site}: " if site else "") + detail)
        if new["hudu_asset_id"] != t["hudu_asset_id"]:
            hudu_archive.clear_review(conn, t["code"])
        herr = hudu_sync(conn, t["code"], auth.label(user))
        saved = db.get_tag(conn, t["code"])
    return {"tag": _tag_info(saved), "hudu_error": herr or ""}
