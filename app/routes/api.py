"""JSON API for other systems, authenticated with API keys (issue #42). See docs/API.md."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import apikeys, codes, db, hudu, hudu_archive, labels, log
from ..tagops import hudu_sync
from ..web import PUBLIC_BASE_URL, clean, hudu_link

router = APIRouter(prefix="/api/v1")
_log = log.get("api")
EDITABLE = ("device_name", "model", "serial", "notes", "hudu_asset_id", "hudu_asset_url")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def error_response(exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": exc.message}, status_code=exc.status)


def _key(request: Request, write: bool = False):
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise ApiError(401, "Send an API key as 'Authorization: Bearer <key>'.")
    with db.connection() as conn:
        row = apikeys.lookup(conn, auth[7:].strip())
    if not row:
        raise ApiError(401, "That API key isn't valid or has been revoked.")
    if apikeys.rate_limited(row["id"]):
        raise ApiError(429, f"Too many requests: at most {apikeys.PER_MINUTE} a minute per key.")
    if write and row["scope"] != "write":
        raise ApiError(403, "This API key is read-only.")
    return row


def _actor(key) -> str:
    return f"API: {key['name']}"


def tag_json(t) -> dict:
    return {
        "code": t["code"],
        "url": f"{PUBLIC_BASE_URL}/{t['code']}",
        "status": t["status"],
        "format": t["format"],
        "format_name": labels.FORMATS.get(t["format"], {}).get("name", t["format"]),
        "client": {"id": t["client_id"], "name": t["client_name"]},
        "device_name": t["device_name"] or "",
        "model": t["model"] or "",
        "serial": t["serial"] or "",
        "notes": t["notes"] or "",
        "hudu_asset_id": t["hudu_asset_id"],
        "hudu_asset_url": t["hudu_asset_url"] or "",
        "printed_count": t["printed_count"],
        "printed_at": t["printed_at"],
        "created_at": t["created_at"],
        "updated_at": t["updated_at"],
    }


def _get(conn, code: str):
    canon = codes.parse(code)
    tag = db.get_tag(conn, canon) if canon else None
    if not tag:
        raise ApiError(404, f"No tag {code!r}.")
    return tag


@router.get("/tags/{code}")
def api_tag(request: Request, code: str):
    _key(request)
    with db.connection() as conn:
        return tag_json(_get(conn, code))


@router.get("/tags")
def api_tags(request: Request, client: str = "", serial: str = "", status: str = "", hudu_asset_id: str = ""):
    _key(request)
    where: list[str] = []
    args: list[object] = []
    if client.isdigit():
        where.append("t.client_id = ?")
        args.append(int(client))
    if serial.strip():
        where.append("UPPER(REPLACE(REPLACE(t.serial, ' ', ''), '-', '')) = ?")
        args.append(serial.strip().replace(" ", "").replace("-", "").upper())
    if status in ("active", "unassigned", "retired"):
        where.append("t.status = ?")
        args.append(status)
    if hudu_asset_id.isdigit():
        where.append("t.hudu_asset_id = ?")
        args.append(int(hudu_asset_id))
    sql = db.TAG_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY t.code LIMIT 500"
    with db.connection() as conn:
        return {"tags": [tag_json(t) for t in conn.execute(sql, args)]}


@router.patch("/tags/{code}")
async def api_update(request: Request, code: str):
    key = _key(request, write=True)
    try:
        body = await request.json()
    except ValueError as e:
        raise ApiError(400, "Send a JSON object.") from e
    if not isinstance(body, dict) or not body:
        raise ApiError(400, "Send a JSON object with the fields to change.")
    unknown = sorted(set(body) - set(EDITABLE))
    if unknown:
        raise ApiError(400, f"These fields can't be changed: {', '.join(unknown)}. Editable: {', '.join(EDITABLE)}.")
    with db.connection() as conn:
        tag = _get(conn, code)
        if tag["status"] == "retired":
            raise ApiError(409, "This tag is retired. Restore it in TagDesk first.")
        new = {k: tag[k] for k in EDITABLE}
        for k in ("device_name", "model", "serial"):
            if k in body:
                new[k] = clean(str(body[k] or ""), 120)
        if "notes" in body:
            new["notes"] = clean(str(body["notes"] or ""), 2000)
        if "hudu_asset_id" in body:
            if body["hudu_asset_id"] in (None, ""):
                new["hudu_asset_id"], new["hudu_asset_url"] = None, ""
            elif not hudu.configured():
                raise ApiError(409, "Hudu isn't set up, so tags can't be linked to Hudu assets.")
            elif str(body["hudu_asset_id"]).isdigit():
                new["hudu_asset_id"] = int(body["hudu_asset_id"])
                new["hudu_asset_url"] = hudu_link(str(body.get("hudu_asset_url") or ""))
            else:
                raise ApiError(400, "hudu_asset_id must be a number, or null to unlink.")
        changes = [f"{k.replace('_', ' ')}: {tag[k] or '—'} → {new[k] or '—'}" for k in EDITABLE if new[k] != tag[k]]
        if changes:
            status = tag["status"]
            if status == "unassigned" and (new["serial"] or new["device_name"] or new["hudu_asset_id"]):
                status = "active"
            conn.execute(
                "UPDATE tags SET device_name=?, model=?, serial=?, notes=?, hudu_asset_id=?, hudu_asset_url=?, "
                "status=?, updated_at=? WHERE code=?",
                (*[new[k] for k in EDITABLE], status, db.now(), tag["code"]),
            )
            db.log(conn, tag["code"], _actor(key), "edited", "; ".join(changes))
            if new["hudu_asset_id"] != tag["hudu_asset_id"]:
                hudu_archive.clear_review(conn, tag["code"])
            hudu_sync(conn, tag["code"], _actor(key))
        return tag_json(db.get_tag(conn, tag["code"]))


@router.post("/tags/{code}/retire")
async def api_retire(request: Request, code: str):
    key = _key(request, write=True)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    reason = clean(str((body or {}).get("reason") or ""), 300) if isinstance(body, dict) else ""
    with db.connection() as conn:
        tag = _get(conn, code)
        if tag["status"] != "retired":
            conn.execute("UPDATE tags SET status='retired', updated_at=? WHERE code=?", (db.now(), tag["code"]))
            db.log(conn, tag["code"], _actor(key), "retired", reason)
            hudu_archive.clear_review(conn, tag["code"])
            hudu_sync(conn, tag["code"], _actor(key))
        return tag_json(db.get_tag(conn, tag["code"]))
