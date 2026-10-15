"""API keys and the JSON API (issue #42)."""

import re

from fastapi.testclient import TestClient

from app import apikeys, db
from app.main import app
from tests.test_app import login


def _admin():
    c = TestClient(app)
    login(c)
    return c


def _key(scope="write", name="RMM sync"):
    r = _admin().post("/settings/api-keys", data={"name": name, "scope": scope})
    m = re.search(r'value="(td_[A-Za-z0-9_-]+)"', r.text)
    assert m, "key shown once on the created page"
    return m.group(1)


def _tag(conn, **fields):
    conn.execute("INSERT INTO clients(name, label_name, active, updated_at) VALUES('Acme', 'Acme', 1, '')")
    cid = conn.execute("SELECT MAX(id) FROM clients").fetchone()[0]
    return db.issue_tags(conn, 1, cid, "standard", "admin", fields)[0]


def _api(key):
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def test_key_is_stored_hashed_and_listed(conn):
    key = _key()
    row = conn.execute("SELECT * FROM api_keys").fetchone()
    assert key not in str(dict(row)) and row["prefix"] == key[:10]
    page = _admin().get("/settings").text
    assert "RMM sync" in page and key not in page and f"{key[:10]}…" in page


def test_lookup_by_serial_and_code(conn):
    code = _tag(conn, device_name="FW01", serial="AB-12 34")
    api = _api(_key("read"))
    r = api.get("/api/v1/tags", params={"serial": "ab1234"})
    assert r.status_code == 200 and [t["code"] for t in r.json()["tags"]] == [code]
    t = api.get(f"/api/v1/tags/{code.lower()}").json()
    assert t["device_name"] == "FW01" and t["client"]["name"] == "Acme" and t["url"].endswith(code)
    assert conn.execute("SELECT last_used_at FROM api_keys").fetchone()[0]


def test_read_key_cannot_write(conn):
    code = _tag(conn)
    r = _api(_key("read")).patch(f"/api/v1/tags/{code}", json={"serial": "X"})
    assert r.status_code == 403 and "read-only" in r.json()["error"]


def test_update_logs_under_key_name_and_assigns(conn):
    code = _tag(conn)
    api = _api(_key())
    r = api.patch(f"/api/v1/tags/{code}", json={"serial": "SN-9", "device_name": "NAS01"})
    assert r.status_code == 200 and r.json()["serial"] == "SN-9" and r.json()["status"] == "active"
    ev = conn.execute("SELECT actor, detail FROM events WHERE code=? AND action='edited'", (code,)).fetchone()
    assert ev["actor"] == "API: RMM sync" and "SN-9" in ev["detail"]


def test_update_rejects_unknown_fields_and_hudu_when_off(conn):
    code = _tag(conn)
    api = _api(_key())
    assert api.patch(f"/api/v1/tags/{code}", json={"status": "retired"}).status_code == 400
    assert api.patch(f"/api/v1/tags/{code}", json={"hudu_asset_id": 5}).status_code == 409
    assert api.patch(f"/api/v1/tags/{code}", content=b"not json").status_code == 400


def test_retire(conn):
    code = _tag(conn, device_name="Old")
    r = _api(_key()).post(f"/api/v1/tags/{code}/retire", json={"reason": "disposed"})
    assert r.json()["status"] == "retired"
    ev = conn.execute("SELECT actor, detail FROM events WHERE code=? AND action='retired'", (code,)).fetchone()
    assert ev["actor"] == "API: RMM sync" and ev["detail"] == "disposed"


def test_revoked_or_missing_key_is_refused(conn):
    key = _key()
    _tag(conn)
    assert TestClient(app).get("/api/v1/tags").status_code == 401
    assert _api("td_nope").get("/api/v1/tags").status_code == 401
    kid = conn.execute("SELECT id FROM api_keys").fetchone()[0]
    _admin().post(f"/settings/api-keys/{kid}/revoke")
    assert _api(key).get("/api/v1/tags").status_code == 401


def test_rate_limit(conn, monkeypatch):
    monkeypatch.setattr(apikeys, "PER_MINUTE", 3)
    apikeys._hits.clear()
    api = _api(_key("read"))
    codes = [api.get("/api/v1/tags").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    apikeys._hits.clear()


def test_only_admins_manage_keys(conn):
    from app import auth, settings

    conn.execute(
        "INSERT INTO users(username, email, role, password, created_at) VALUES('sam', '', 'user', ?, '')",
        (auth.hash_password("a-long-password-1"),),
    )
    settings.mark_setup_done()
    web = TestClient(app)
    web.post("/login", data={"username": "sam", "password": "a-long-password-1", "next": "/"})
    assert web.post("/settings/api-keys", data={"name": "x", "scope": "write"}).status_code == 403
