"""Tag a site from a phone (issue #34)."""

import json

from fastapi.testclient import TestClient

from app import coverage, db
from app.main import app
from tests.test_app import login


def _setup(conn, hudu_id=None):
    conn.execute(
        "INSERT INTO clients(hudu_id, name, label_name, active, updated_at) VALUES(?, 'Acme', 'Acme', 1, '')",
        (hudu_id,),
    )
    cid = conn.execute("SELECT MAX(id) FROM clients").fetchone()[0]
    return cid, db.issue_tags(conn, 2, cid, "standard", "admin")


def _web():
    c = TestClient(app)
    login(c)
    return c


def test_page_and_lookup(conn):
    cid, (code, _) = _setup(conn)
    web = _web()
    page = web.get("/tag-a-site").text
    assert "Start tagging" in page and "jsQR.js" in page and "site.js" in page
    t = web.get(f"/api/site/tag/{code.lower()}").json()
    assert t["code"] == code and t["status"] == "unassigned" and t["client_id"] == cid
    assert web.get("/api/site/tag/ZZZZZ").status_code == 404


def test_assign_saves_and_logs_site(conn):
    cid, (code, _) = _setup(conn)
    r = _web().post(
        "/api/site/assign",
        json={"code": code, "client_id": cid, "site": "Head office", "device_name": "SW01", "serial": "S1"},
    )
    assert r.status_code == 200 and r.json()["tag"]["status"] == "active"
    ev = conn.execute("SELECT action, detail FROM events WHERE code=? ORDER BY id DESC", (code,)).fetchone()
    assert ev["action"] == "assigned on site" and ev["detail"].startswith("Head office:")


def test_assign_refuses_wrong_client_assigned_retired_and_empty(conn):
    cid, (code, other) = _setup(conn)
    web = _web()
    assert (
        web.post("/api/site/assign", json={"code": code, "client_id": cid + 99, "device_name": "X"}).status_code == 409
    )
    assert web.post("/api/site/assign", json={"code": code, "client_id": cid}).status_code == 400
    web.post("/api/site/assign", json={"code": code, "client_id": cid, "device_name": "A"})
    r = web.post("/api/site/assign", json={"code": code, "client_id": cid, "device_name": "B"})
    assert r.status_code == 409 and "already on A" in r.json()["error"]
    assert (
        web.post(
            "/api/site/assign", json={"code": code, "client_id": cid, "device_name": "B", "replace": True}
        ).status_code
        == 200
    )
    conn.execute("UPDATE tags SET status='retired' WHERE code=?", (other,))
    assert web.post("/api/site/assign", json={"code": other, "client_id": cid, "device_name": "C"}).status_code == 409


def test_devices_from_coverage_cache(fake, conn):
    cid, (code, _) = _setup(conn, hudu_id=3)
    coverage.refresh(conn, conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone())
    web = _web()
    d = web.get(f"/api/site/devices?client_id={cid}").json()
    assert {a["name"] for a in d["devices"]} == {"FW01", "NAS01"} and d["fetched"]
    r = web.post("/api/site/assign", json={"code": code, "client_id": cid, "device_name": "FW01", "hudu_asset_id": 10})
    assert r.json()["tag"]["hudu_asset_id"] == 10
    assert {a["name"] for a in web.get(f"/api/site/devices?client_id={cid}").json()["devices"]} == {"NAS01"}


def test_devices_empty_without_hudu(conn):
    cid, _ = _setup(conn)
    assert _web().get(f"/api/site/devices?client_id={cid}").json() == {"devices": [], "fetched": None}


def test_site_api_needs_sign_in(conn):
    _, (code, _) = _setup(conn)
    anon = TestClient(app)
    assert anon.get(f"/api/site/tag/{code}", follow_redirects=False).status_code == 303
    assert anon.post("/api/site/assign", json={"code": code}, follow_redirects=False).status_code == 303


def test_manifest_icons_and_camera_permission():
    c = TestClient(app)
    m = c.get("/manifest.webmanifest")
    assert json.loads(m.text)["start_url"] == "/tag-a-site"
    icon = c.get("/icon-192.png")
    assert icon.status_code == 200 and icon.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert c.get("/icon-64.png").status_code == 404
    assert "camera=(self)" in c.get("/healthz").headers["permissions-policy"]
