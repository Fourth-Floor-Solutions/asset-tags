"""Hudu access: companies and assets for lookups, plus writing the tag code
into each linked asset's "Asset Tag" custom field.

Calls are paced because bursts trip Hudu's transient rate block.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

import httpx

TIMEOUT = 20
PAGE_SIZE = 100
PAUSE = 0.4


class HuduError(Exception):
    pass


def api_key():
    from . import settings

    return settings.secret("hudu_api_key")


def configured() -> bool:
    return bool(base_url() and api_key())


def base_url() -> str:
    from . import settings

    return settings.get("hudu_url").rstrip("/")


def _request(method: str, path: str, params: Optional[dict] = None, body: Optional[dict] = None) -> object:
    if not configured():
        raise HuduError("Hudu isn't set up. Add its address and API key in Settings.")
    try:
        r = httpx.request(
            method,
            f"{base_url()}/api/v1{path}",
            params=params,
            json=body,
            headers={"x-api-key": api_key(), "Accept": "application/json"},
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise HuduError(f"Couldn't reach Hudu: {e}") from e
    if r.status_code in (401, 403):
        raise HuduError("Hudu refused the API key (or the request came from outside the allowed network).")
    if r.status_code >= 400:
        raise HuduError(f"Hudu returned HTTP {r.status_code}.")
    return r.json() if r.content else {}


def _get(path: str, params: dict) -> object:
    return _request("GET", path, params=params)


def list_companies() -> list:
    out, page = [], 1
    while True:
        data = _get("/companies", {"page": page, "page_size": PAGE_SIZE})
        batch = data.get("companies", []) if isinstance(data, dict) else []
        out.extend(batch)
        if len(batch) < PAGE_SIZE:
            return out
        page += 1
        time.sleep(PAUSE)


def _abs(url: Optional[str]) -> str:
    if not url:
        return ""
    return url if url.startswith("http") else f"{base_url()}{url}"


def _field(asset: dict, word: str) -> str:
    for f in asset.get("fields") or []:
        if word in (f.get("label") or "").lower() and f.get("value"):
            return str(f["value"]).strip()
    return ""


def _summary(a: dict) -> dict:
    return {
        "id": a.get("id"),
        "name": a.get("name") or "",
        # Many layouts keep serial/model in custom fields, not the primary_* columns.
        "serial": a.get("primary_serial") or _field(a, "serial"),
        "model": a.get("primary_model") or _field(a, "model"),
        "type": a.get("asset_type") or "",
        "url": _abs(a.get("url")),
    }


SCAN_PAGES = 5


def search_assets(company_id: int, query: str) -> list:
    """Assets for one company matching `query`.

    Hudu's `search` matches names but not custom fields, so a serial number
    finds nothing. When it comes back empty, page through the company's
    assets (up to SCAN_PAGES pages) and match name, serial and model here.
    """
    base = {"company_id": company_id, "archived": "false"}
    if not query:
        data = _get("/assets", dict(base, page_size=25))
        return [_summary(a) for a in data.get("assets", [])]
    data = _get("/assets", dict(base, search=query, page_size=25))
    found = [a for a in data.get("assets", []) if a.get("company_id") in (None, company_id)]
    if found:
        return [_summary(a) for a in found][:25]
    q = query.lower()
    out = []
    for page in range(1, SCAN_PAGES + 1):
        time.sleep(PAUSE)
        batch = _get("/assets", dict(base, page=page, page_size=PAGE_SIZE)).get("assets", [])
        for a in batch:
            s = _summary(a)
            if q in f"{s['name']} {s['serial']} {s['model']}".lower():
                out.append(s)
        if len(batch) < PAGE_SIZE or len(out) >= 25:
            break
    return out[:25]


# ---------- Asset Tag field write-back ----------

def tag_label() -> str:
    from . import settings

    return settings.get("hudu_tag_field") or "Asset Tag"


def write_enabled() -> bool:
    from . import settings

    return configured() and settings.get("hudu_write") == "1"


def field_key(label: str) -> str:
    # Hudu's custom_fields keys are the label lower-cased with spaces as
    # underscores; punctuation (e.g. a trailing colon) is kept.
    return label.strip().lower().replace(" ", "_")


def get_asset(asset_id: int) -> Optional[dict]:
    data = _get("/assets", {"id": asset_id})
    assets = data.get("assets", []) if isinstance(data, dict) else []
    return assets[0] if assets else None


_layout_cache: dict = {}


def layout_has_tag_field(layout_id: int) -> bool:
    if layout_id not in _layout_cache:
        data = _get(f"/asset_layouts/{layout_id}", {})
        layout = data.get("asset_layout", data) if isinstance(data, dict) else {}
        labels = {f.get("label", "").strip().lower() for f in layout.get("fields", []) if not f.get("is_destroyed")}
        _layout_cache[layout_id] = tag_label().lower() in labels
        time.sleep(PAUSE)
    return _layout_cache[layout_id]


def _values(asset: dict) -> dict:
    return {field_key(f.get("label", "")): f.get("value") for f in asset.get("fields") or []}


def _norm(v) -> str:
    """Comparable form of a field value.

    Linked-asset fields (e.g. Location) come back as a JSON string whose key
    order and extra keys (url) can change on save; compare them by linked ids.
    """
    if v is None:
        return ""
    s = str(v)
    if s.startswith("[{") or s.startswith("{"):
        try:
            data = json.loads(s)
            items = data if isinstance(data, list) else [data]
            if all(isinstance(i, dict) and "id" in i for i in items):
                return "links:" + ",".join(str(i["id"]) for i in sorted(items, key=lambda i: str(i["id"])))
            return json.dumps(data, sort_keys=True)
        except ValueError:
            pass
    return s


def _put(asset: dict, values: dict) -> None:
    body = {"name": asset["name"], "asset_layout_id": asset["asset_layout_id"],
            "custom_fields": [{k: v} for k, v in values.items()]}
    for k in ("primary_serial", "primary_model", "primary_manufacturer", "primary_mail"):
        if asset.get(k) is not None:
            body[k] = asset[k]
    _request("PUT", f"/companies/{asset['company_id']}/assets/{asset['id']}", body={"asset": body})
    time.sleep(PAUSE)


def set_tag(asset_id: int, value: str, only_if: Optional[str] = None) -> str:
    """Write `value` (a code, or "" to clear) into the asset's Asset Tag field.

    Hudu's PUT replaces every custom field, so all current values are re-sent
    unchanged. The asset is read back afterwards; if any other field moved,
    the original values are restored and HuduError is raised.
    With `only_if`, the field is only changed while it still holds that code,
    so clearing never wipes a value someone else put there.
    Returns a short description of what happened.
    """
    asset = get_asset(asset_id)
    time.sleep(PAUSE)
    if not asset:
        raise HuduError(f"Hudu asset {asset_id} no longer exists.")
    if not layout_has_tag_field(asset["asset_layout_id"]):
        return f"skipped: the {asset.get('asset_type') or 'asset'} layout has no {tag_label()} field"
    key = field_key(tag_label())
    before = _values(asset)
    current = _norm(before.get(key))
    if current == value:
        return "already up to date"
    if only_if is not None and current != only_if:
        return f"left alone: Hudu holds '{current}', not {only_if}"
    wanted = {k: v for k, v in before.items() if v is not None}
    wanted[key] = value
    _put(asset, wanted)
    after_asset = get_asset(asset_id) or {}
    after = _values(after_asset)
    changed = [k for k in set(before) | set(after)
               if k != key and _norm(before.get(k)) != _norm(after.get(k))]
    if changed or _norm(after.get(key)) != value:
        restore = {k: v for k, v in before.items() if v is not None}
        restore[key] = before.get(key) or ""   # an omitted key is left as is, so reset it explicitly
        _put(asset, restore)
        raise HuduError("Hudu didn't keep the other fields as sent (" + ", ".join(sorted(changed)) +
                        "). The original values were written back; nothing was changed.")
    return "cleared" if value == "" else "written"
