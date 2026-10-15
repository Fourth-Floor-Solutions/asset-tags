"""Tag coverage (issue #33): which of a client's Hudu devices have a tag.

Each client's Hudu assets are fetched on demand (Refresh) and cached in the
`coverage` table, so viewing coverage never calls Hudu and never writes to
it. Asset types that never get a label (licences, contacts, cloud services)
can be left out by layout, in the setting `hudu_coverage_exclude`.
"""

from __future__ import annotations

import json
import threading
import time

from . import db, hudu, log, settings

_log = log.get("hudu")
_refreshing = threading.Lock()


def excluded_layouts() -> set[int]:
    raw = settings.get("hudu_coverage_exclude") or ""
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


def refresh(conn, client) -> str:
    """Fetch one client's assets from Hudu into the cache. Returns an error message, or ''."""
    try:
        assets = hudu.company_assets(client["hudu_id"])
        error = ""
    except hudu.HuduError as e:
        assets, error = None, str(e)
    if assets is None:
        conn.execute(
            "INSERT INTO coverage(client_id, refreshed_at, error) VALUES(?,?,?) "
            "ON CONFLICT(client_id) DO UPDATE SET error=excluded.error",
            (client["id"], db.now(), error),
        )
        _log.warning("coverage refresh for %s failed: %s", client["name"], error)
    else:
        conn.execute(
            "INSERT INTO coverage(client_id, refreshed_at, assets, error) VALUES(?,?,?,'') "
            "ON CONFLICT(client_id) DO UPDATE SET refreshed_at=excluded.refreshed_at, assets=excluded.assets, error=''",
            (client["id"], db.now(), json.dumps(assets)),
        )
    return error


def hudu_clients(conn) -> list:
    return conn.execute(
        "SELECT * FROM clients WHERE active = 1 AND hudu_id IS NOT NULL ORDER BY is_internal DESC, name COLLATE NOCASE"
    ).fetchall()


def refresh_all_in_background() -> bool:
    """Refresh every Hudu-linked client in a background thread. False if one is already running."""
    if not _refreshing.acquire(blocking=False):
        return False

    def work() -> None:
        try:
            with db.connection() as conn:
                done = 0
                for n, c in enumerate(hudu_clients(conn)):
                    if n:
                        time.sleep(hudu.PAUSE)
                    if refresh(conn, c):
                        break  # Hudu unreachable or refusing: the rest would fail too
                    done += 1
                _log.info("coverage refreshed for %d clients", done)
        except Exception:
            _log.exception("coverage refresh failed")
        finally:
            _refreshing.release()

    threading.Thread(target=work, name="coverage-refresh", daemon=True).start()
    return True


def refreshing() -> bool:
    return _refreshing.locked()


def _cached(conn, client_id: int):
    return conn.execute("SELECT * FROM coverage WHERE client_id = ?", (client_id,)).fetchone()


def for_client(conn, client, exclude: set[int] | None = None) -> dict:
    """Coverage for one client from the cache: counts, untagged assets, unlinked tags."""
    exclude = excluded_layouts() if exclude is None else exclude
    row = _cached(conn, client["id"])
    tags = conn.execute(
        db.TAG_SELECT + " WHERE t.client_id = ? AND t.status != 'retired' ORDER BY t.code", (client["id"],)
    ).fetchall()
    linked = {t["hudu_asset_id"] for t in tags if t["hudu_asset_id"]}
    unlinked = [t for t in tags if not t["hudu_asset_id"]]
    out = {
        "client": client,
        "checked": row is not None and row["refreshed_at"] is not None,
        "refreshed_at": row["refreshed_at"] if row else None,
        "error": row["error"] if row else "",
        "unlinked": unlinked,
        "assets": 0,
        "tagged": 0,
        "untagged": [],
        "pct": None,
    }
    if row:
        assets = [a for a in json.loads(row["assets"] or "[]") if a.get("layout_id") not in exclude]
        out["assets"] = len(assets)
        out["untagged"] = sorted(
            (a for a in assets if a["id"] not in linked), key=lambda a: (a["type"].lower(), a["name"].lower())
        )
        out["tagged"] = out["assets"] - len(out["untagged"])
        out["pct"] = round(100 * out["tagged"] / out["assets"]) if out["assets"] else None
    return out


def layouts_seen(conn) -> list[dict]:
    """Every Hudu layout (asset type) in the cache, with how many assets use it, for the exclude list."""
    counts: dict[int, dict] = {}
    for row in conn.execute("SELECT assets FROM coverage"):
        for a in json.loads(row["assets"] or "[]"):
            lid = a.get("layout_id")
            if lid is None:
                continue
            entry = counts.setdefault(lid, {"id": lid, "name": a.get("type") or f"Layout {lid}", "assets": 0})
            entry["assets"] += 1
    return sorted(counts.values(), key=lambda x: x["name"].lower())
