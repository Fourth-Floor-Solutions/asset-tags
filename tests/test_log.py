"""The application log: sign-ins and tag events are logged; passwords never are."""

import logging

from fastapi.testclient import TestClient

from app import log
from app.main import app
from tests.test_app import login


def test_sign_ins_are_logged_without_passwords(caplog):
    caplog.set_level(logging.INFO, logger="tagdesk")
    c = TestClient(app)
    c.post("/login", data={"username": "admin", "password": "wrong-password-xyz", "next": "/"})
    login(c)
    text = caplog.text
    assert "failed sign-in for 'admin'" in text and "admin signed in" in text
    assert "wrong-password-xyz" not in text and "test-password-123" not in text


def test_tag_events_and_setting_changes_are_logged(caplog, conn):
    from app import settings

    caplog.set_level(logging.INFO, logger="tagdesk")
    c = TestClient(app)
    login(c)
    cid = conn.execute("SELECT id FROM clients LIMIT 1").fetchone()
    if cid is None:
        conn.execute("INSERT INTO clients(name, label_name, active, updated_at) VALUES('Acme','Acme',1,'')")
        cid = conn.execute("SELECT id FROM clients LIMIT 1").fetchone()
    c.post("/tags/batch", data={"client_id": cid[0], "format": "standard", "count": 1})
    assert any(r.name == "tagdesk.tags" and " issued by " in r.getMessage() for r in caplog.records)
    settings.audit("admin", "accent", "#5b8def → #ff0000")
    assert "accent changed by admin" in caplog.text


def test_setup_is_idempotent_and_honours_log_level(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "warning")
    log.setup()
    log.setup()
    root = logging.getLogger("tagdesk")
    assert len(root.handlers) == 1 and root.level == logging.WARNING
    monkeypatch.setenv("LOG_LEVEL", "info")
    log.setup()
