"""Export the Tags list as CSV (issue #37)."""

import csv
import io

from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.test_app import login


def _setup(conn):
    for name in ("Acme Ltd", "Brightside"):
        conn.execute("INSERT INTO clients(name, label_name, active, updated_at) VALUES(?, ?, 1, '')", (name, name))
    ids = [r[0] for r in conn.execute("SELECT id FROM clients ORDER BY id DESC LIMIT 2")]
    b, a = ids
    db.issue_tags(conn, 2, a, "standard", "admin", {"device_name": "FW01", "serial": "=cmd|' /C calc'!A0"})
    db.issue_tags(conn, 1, b, "slim", "admin")
    return a, b


def _rows(resp):
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.text.startswith("﻿")
    return list(csv.reader(io.StringIO(resp.text.lstrip("﻿"))))


def test_export_all_and_filtered(conn):
    a, _ = _setup(conn)
    web = TestClient(app)
    login(web)
    rows = _rows(web.get("/tags.csv"))
    assert rows[0][:4] == ["Code", "Status", "Format", "Client"] and len(rows) - 1 >= 3
    r = web.get(f"/tags.csv?client_id={a}")
    only_a = _rows(r)
    assert len(only_a) == 3 and {row[3] for row in only_a[1:]} == {"Acme Ltd"}
    assert "tags-Acme-Ltd-" in r.headers["content-disposition"]
    assert only_a[1][1] == "Assigned" and only_a[1][2] == "Standard"


def test_export_cells_cant_run_formulas(conn):
    a, _ = _setup(conn)
    web = TestClient(app)
    login(web)
    rows = _rows(web.get(f"/tags.csv?client_id={a}"))
    assert all(row[6].startswith("'=") for row in rows[1:])


def test_export_needs_sign_in():
    r = TestClient(app).get("/tags.csv", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]


def test_export_button_keeps_filters(conn):
    a, _ = _setup(conn)
    web = TestClient(app)
    login(web)
    page = web.get(f"/?client_id={a}&status=active").text
    assert f"/tags.csv?q=&amp;client_id={a}&amp;status=active" in page
