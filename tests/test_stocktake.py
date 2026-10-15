"""Stocktake: last seen, and tags not seen recently (issue #39)."""

from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.test_app import login


def _setup(conn, n=3):
    conn.execute("INSERT INTO clients(name, label_name, active, updated_at) VALUES('Acme', 'Acme', 1, '')")
    cid = conn.execute("SELECT MAX(id) FROM clients").fetchone()[0]
    tags = db.issue_tags(conn, n, cid, "standard", "admin")
    for i, code in enumerate(tags):
        conn.execute("UPDATE tags SET status='active', device_name=? WHERE code=?", (f"DEV{i}", code))
    return cid, tags


def _web():
    c = TestClient(app)
    login(c)
    return c


def test_seen_records_who_where_and_history(conn):
    cid, (a, b, c) = _setup(conn)
    web = _web()
    r = web.post("/api/site/seen", json={"code": a, "client_id": cid, "site": "Head office"})
    assert r.status_code == 200 and r.json()["device_name"] == "DEV0" and not r.json()["other_client"]
    t = db.get_tag(conn, a)
    assert t["last_seen_at"] and t["last_seen_by"] == "admin" and t["last_seen_site"] == "Head office"
    assert conn.execute("SELECT COUNT(*) FROM events WHERE code=? AND action='seen'", (a,)).fetchone()[0] == 1
    assert "Head office" in web.get(f"/tags/{a}").text


def test_finish_lists_unscanned(conn):
    cid, (a, b, c) = _setup(conn)
    web = _web()
    started = db.now()
    for code in (a, c):
        web.post("/api/site/seen", json={"code": code, "client_id": cid})
    unseen = web.get(f"/api/site/unseen?client_id={cid}&since={started}").json()["tags"]
    assert [t["code"] for t in unseen] == [b]


def test_other_clients_tag_is_flagged(conn):
    cid, (a, *_) = _setup(conn)
    r = _web().post("/api/site/seen", json={"code": a, "client_id": cid + 50})
    assert r.json()["other_client"] is True


def test_opening_scan_link_signed_in_counts_as_seen(conn):
    _, (a, *_) = _setup(conn)
    web = _web()
    web.get(f"/{a}", follow_redirects=False)
    assert db.get_tag(conn, a)["last_seen_at"]
    assert conn.execute("SELECT COUNT(*) FROM events WHERE code=? AND action='seen'", (a,)).fetchone()[0] == 0
    anon = TestClient(app)
    _, (z, *_) = _setup(conn, 1)
    anon.get(f"/{z}")
    assert db.get_tag(conn, z)["last_seen_at"] is None  # the public don't count


def test_not_seen_page(conn):
    cid, (a, b, c) = _setup(conn)
    conn.execute("UPDATE tags SET created_at='2024-01-01T00:00:00+00:00'")
    conn.execute("UPDATE tags SET last_seen_at=? WHERE code=?", (db.now(), a))
    page = _web().get(f"/not-seen?client_id={cid}&months=12").text
    assert b in page and c in page and f"/tags/{a}" not in page and "Never" in page


def test_stocktake_page():
    page = _web().get("/stocktake").text
    assert "Start stocktake" in page and 'data-mode="stocktake"' in page
