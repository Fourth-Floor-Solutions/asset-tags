"""Warn before label tape runs out (issue #40)."""

import logging

from fastapi.testclient import TestClient

from app import db, printer, usage
from app.main import app
from tests.test_app import login


def _printer(conn, last_tape="18|yellow"):
    conn.execute(
        "INSERT INTO printers(name, host, nudge_dots, is_default, created_at, last_counter_cm, last_tape) "
        "VALUES('Desk printer', '192.0.2.10', 0, 1, '', 1000, ?)",
        (last_tape,),
    )
    return conn.execute("SELECT * FROM printers WHERE name='Desk printer'").fetchone()


def _cassette(conn, p, used_cm, tape="18|yellow"):
    conn.execute(
        "INSERT INTO cassettes(printer_id, tape, used_cm, length_cm, fitted_at) VALUES(?,?,?,800,'')",
        (p["id"], tape, used_cm),
    )


def test_low_loaded_cassette_shows_banner_on_staff_pages_only(conn):
    p = _printer(conn)
    _cassette(conn, p, 750)  # 0.5 m left
    _cassette(conn, p, 790, tape="12|yellow")  # low, but not the one loaded
    assert usage.low_cassettes(conn) == [{"printer": "Desk printer", "tape": "18 mm yellow", "left_m": 0.5}]
    web = TestClient(app)
    login(web)
    page = web.get("/").text
    assert "Tape is low in Desk printer (18 mm yellow): about 0.5 m left" in page
    conn.execute("INSERT INTO clients(name, label_name, active, updated_at) VALUES('Acme', 'Acme', 1, '')")
    code = db.issue_tags(conn, 1, conn.execute("SELECT MAX(id) FROM clients").fetchone()[0], "standard", "x")[0]
    assert "Tape is low" not in TestClient(app).get(f"/{code}").text  # public scan page


def test_empty_cassette_says_out(conn):
    _cassette(conn, _printer(conn), 820)
    web = TestClient(app)
    login(web)
    assert "Tape is out in Desk printer (18 mm yellow)." in web.get("/").text


def test_no_banner_with_plenty_left(conn):
    _cassette(conn, _printer(conn), 100)
    assert usage.low_cassettes(conn) == []


def test_going_low_is_logged_once_and_reset_by_new_cassette(conn, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="tagdesk")
    p = _printer(conn)
    _cassette(conn, p, 650)
    readings = iter([1060, 1100, 1110])  # cm on the printer's lifetime counter
    monkeypatch.setattr(printer, "counter_cm", lambda host: next(readings))
    st = {"tape_mm": 18, "tape_colour": "yellow"}
    for _ in range(3):
        usage.track(conn, dict(p), st)
    assert sum("tape is low in Desk printer" in r.getMessage() for r in caplog.records) == 1
    cid = conn.execute("SELECT id FROM cassettes WHERE printer_id=?", (p["id"],)).fetchone()[0]
    web = TestClient(app)
    login(web)
    web.post(f"/cassettes/{cid}/new")
    row = conn.execute("SELECT used_cm, low_logged FROM cassettes WHERE id=?", (cid,)).fetchone()
    assert row["used_cm"] == 0 and row["low_logged"] == 0
