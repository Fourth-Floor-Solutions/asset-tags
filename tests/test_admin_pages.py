"""Users, clients, cassettes and printer management pages."""

from fastapi.testclient import TestClient

from app import db, printer, settings, usage, web
from app.main import app
from tests.test_app import login


def _admin():
    c = TestClient(app)
    login(c)
    return c


def test_users_add_role_reset_delete(conn):
    c = _admin()
    c.post(
        "/users/new",
        data={"username": "sam", "email": "sam@example.com", "role": "user", "password": "a-long-password"},
    )
    uid = conn.execute("SELECT id FROM users WHERE username='sam'").fetchone()["id"]
    c.post(f"/users/{uid}/role", data={"role": "admin"})
    assert conn.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()["role"] == "admin"
    c.post(f"/users/{uid}/password", data={"password": "short"})  # refused: under 12 characters
    c.post(f"/users/{uid}/name", data={"name": "Sam Taylor"})
    assert conn.execute("SELECT display_name FROM users WHERE id=?", (uid,)).fetchone()[0] == "Sam Taylor"
    c.post(f"/users/{uid}/delete")
    assert conn.execute("SELECT COUNT(*) FROM users WHERE id=?", (uid,)).fetchone()[0] == 0


def test_cannot_remove_own_admin_or_delete_self(conn):
    c = _admin()
    me = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
    c.post(f"/users/{me}/role", data={"role": "user"})
    c.post(f"/users/{me}/delete")
    row = conn.execute("SELECT role FROM users WHERE id=?", (me,)).fetchone()
    assert row and row["role"] == "admin"


def test_users_page_is_admin_only():
    c = _admin()
    c.post("/users/new", data={"username": "u1", "role": "user", "password": "a-long-password"})
    u = TestClient(app)
    u.post("/login", data={"username": "u1", "password": "a-long-password", "next": "/"})
    assert u.get("/users").status_code == 403 and u.get("/settings").status_code == 403


def test_clients_add_and_label_name(conn):
    c = _admin()
    c.post("/clients/new", data={"name": "Example Ltd", "label_name": "Example"})
    cid = conn.execute("SELECT id FROM clients WHERE name='Example Ltd'").fetchone()["id"]
    c.post(f"/clients/{cid}", data={"label_name": "Example UK", "active": "1"})
    assert conn.execute("SELECT label_name FROM clients WHERE id=?", (cid,)).fetchone()[0] == "Example UK"


def test_cassette_new_and_set(monkeypatch, conn):
    c = _admin()
    conn.execute("INSERT INTO printers(name, host, created_at) VALUES('P','192.0.2.9',?)", (db.now(),))
    pid = conn.execute("SELECT id FROM printers").fetchone()["id"]
    st = {"tape_mm": 18, "tape_colour": "yellow", "errors": []}
    summ = usage.summary(conn, pid, st)
    c.post(f"/cassettes/{summ['id']}", data={"used_m": "2.5", "length_m": "8"})
    assert usage.summary(conn, pid, st)["used_cm"] == 250
    c.post(f"/cassettes/{summ['id']}/new")
    assert usage.summary(conn, pid, st)["used_cm"] == 0


def test_printer_edit_default_delete(monkeypatch, conn):
    c = _admin()
    monkeypatch.setattr(printer, "status", lambda host: (_ for _ in ()).throw(printer.PrinterError("offline")))
    c.post("/printers/new", data={"name": "One", "address": "192.0.2.1"})
    c.post("/printers/new", data={"name": "Two", "address": "192.0.2.2"})
    two = conn.execute("SELECT id FROM printers WHERE name='Two'").fetchone()["id"]
    c.post(f"/printers/{two}", data={"name": "Two", "address": "192.0.2.3", "dots": "-14"})
    c.post(f"/printers/{two}/default")
    row = conn.execute("SELECT host, nudge_dots, is_default FROM printers WHERE id=?", (two,)).fetchone()
    assert (row["host"], row["nudge_dots"], row["is_default"]) == ("192.0.2.3", -14, 1)
    c.post(f"/printers/{two}/delete")
    assert conn.execute("SELECT is_default FROM printers").fetchone()["is_default"] == 1  # default moved


def test_account_password_change():
    c = _admin()
    c.post("/account/password", data={"current": "test-password-123", "new": "another-long-password"})
    c2 = TestClient(app)
    r = c2.post(
        "/login", data={"username": "admin", "password": "another-long-password", "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303


def test_settings_formats_and_whats_new_render():
    c = _admin()
    assert "Label formats" in c.get("/settings").text and c.get("/changelog").status_code == 200
    assert settings.get("org_name")
    assert web.tag_streams(rows=2, per_row=3) and len(web.tag_streams(rows=2, per_row=3)[0]) == 3


def test_settings_read_once_per_request(monkeypatch):
    c = _admin()
    calls = []
    real = settings._load_all
    monkeypatch.setattr(settings, "_load_all", lambda: calls.append(1) or real())
    assert c.get("/clients").status_code == 200
    assert len(calls) == 1
    # Outside a request nothing is cached: each call reads the database.
    settings.get_all()
    settings.get_all()
    assert len(calls) == 3


def test_duplicate_username_is_refused(conn):
    c = _admin()
    form = {"username": "Admin", "email": "", "role": "user", "password": "a-long-password"}
    r = c.post("/users/new", data=form, follow_redirects=True)
    assert "already exists" in r.text
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
