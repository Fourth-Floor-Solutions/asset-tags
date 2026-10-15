"""Backups (issue #41)."""

import os
import sqlite3

from fastapi.testclient import TestClient

from app import backup, db, secretbox, settings
from app.main import app
from tests.test_app import login


def _admin():
    c = TestClient(app)
    login(c)
    return c


def test_download_is_a_working_copy_and_audited(conn, tmp_path):
    conn.execute("INSERT INTO clients(name, label_name, active, updated_at) VALUES('Acme', 'Acme', 1, '')")
    r = _admin().get("/settings/backup/tagdesk.db")
    assert r.status_code == 200 and "tagdesk-" in r.headers["content-disposition"]
    path = tmp_path / "restored.db"
    path.write_bytes(r.content)
    copy = sqlite3.connect(path)
    try:
        assert copy.execute("SELECT name FROM clients WHERE name='Acme'").fetchone() == ("Acme",)
    finally:
        copy.close()
    assert any(a["setting"] == "backup" for a in settings.recent_audit())


def test_secret_key_download_matches_and_decrypts():
    settings.save_secret("hudu_api_key", "abc123")
    r = _admin().get("/settings/backup/secret.key")
    assert r.status_code == 200
    with open(os.path.join(db.DATA_DIR, secretbox.KEY_FILE), "rb") as f:
        assert r.content == f.read()
    assert settings.secret("hudu_api_key") == "abc123"


def test_key_not_offered_when_set_in_environment(monkeypatch):
    monkeypatch.setenv("TAGDESK_SECRET_KEY", secretbox.Fernet.generate_key().decode())
    web = _admin()
    assert web.get("/settings/backup/secret.key", follow_redirects=False).status_code == 303
    assert "Download secret.key" not in web.get("/settings").text


def test_backups_are_admin_only(conn):
    conn.execute(
        "INSERT INTO users(username, email, role, password, created_at) VALUES('sam', '', 'user', ?, '')",
        (__import__("app.auth", fromlist=["hash_password"]).hash_password("a-long-password-1"),),
    )
    settings.mark_setup_done()
    web = TestClient(app)
    web.post("/login", data={"username": "sam", "password": "a-long-password-1", "next": "/"})
    assert web.get("/settings/backup/tagdesk.db").status_code == 403
    assert web.get("/settings/backup/secret.key").status_code == 403


def test_daily_copies_are_pruned(monkeypatch):
    monkeypatch.setenv("BACKUP_KEEP", "2")
    os.makedirs(backup.folder(), exist_ok=True)
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        open(os.path.join(backup.folder(), f"tagdesk-{day}.db"), "wb").close()
    today = backup.daily()
    names = [b["name"] for b in backup.list_daily()]
    assert os.path.basename(today) == names[0] and len(names) == 2 and "tagdesk-2026-01-03.db" in names


def test_daily_off(monkeypatch):
    monkeypatch.setenv("BACKUP_KEEP", "0")
    assert backup.daily() is None
    assert "Daily copies are off" in _admin().get("/settings").text
