"""The Clients page and syncing clients from Hudu."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import auth, db, hudu
from ..tagops import (
    hudu_needs_sync,
)
from ..web import clean, flash, go, page

router = APIRouter()


@router.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request):
    auth.require_user(request)
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM tags t WHERE t.client_id = c.id AND t.status != 'retired') AS n "
            "FROM clients c ORDER BY c.active DESC, c.is_internal DESC, c.name COLLATE NOCASE"
        ).fetchall()
        pending = sum(1 for t in conn.execute(db.TAG_SELECT) if hudu_needs_sync(t))
    return page(request, "clients.html", clients=rows, hudu_pending=pending, hudu_write=hudu.write_enabled())


@router.post("/clients/sync")
def clients_sync(request: Request):
    auth.require_admin(request)
    try:
        companies = hudu.list_companies()
    except hudu.HuduError as e:
        flash(request, str(e), "bad")
        return go("/clients")
    added = renamed = 0
    with db.connection() as conn:
        for co in companies:
            if co.get("archived"):
                continue
            hid, name = co.get("id"), (co.get("name") or "").strip()
            if not hid or not name:
                continue
            row = conn.execute("SELECT * FROM clients WHERE hudu_id = ?", (hid,)).fetchone()
            if row:
                if row["name"] != name:
                    conn.execute("UPDATE clients SET name=?, updated_at=? WHERE id=?", (name, db.now(), row["id"]))
                    renamed += 1
            else:
                conn.execute(
                    "INSERT INTO clients(hudu_id, name, label_name, is_internal, active, updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (hid, name, (co.get("nickname") or name).strip()[:40], 0, 1, db.now()),
                )
                added += 1
    flash(
        request,
        f"Synced {len(companies)} Hudu companies: {added} added, {renamed} renamed. "
        "Check the label names of new clients.",
    )
    return go("/clients")


@router.post("/clients/new")
def client_new(request: Request, name: str = Form(...), label_name: str = Form("")):
    auth.require_admin(request)
    name = clean(name, 120)
    if name:
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO clients(hudu_id, name, label_name, is_internal, active, updated_at) VALUES(NULL,?,?,0,1,?)",
                (name, clean(label_name, 40) or name[:40], db.now()),
            )
        flash(request, f"Added {name}.")
    return go("/clients")


@router.post("/clients/{cid}")
def client_update(request: Request, cid: int, label_name: str = Form(...), active: str = Form("")):
    auth.require_admin(request)
    label = clean(label_name, 40)
    if not label:
        flash(request, "The label name can't be empty.", "bad")
        return go("/clients")
    with db.connection() as conn:
        conn.execute(
            "UPDATE clients SET label_name=?, active=?, updated_at=? WHERE id=?",
            (label, 1 if active else 0, db.now(), cid),
        )
    flash(request, "Client saved. Tags already printed with the old name are listed as needing a reprint.")
    return go("/clients")
