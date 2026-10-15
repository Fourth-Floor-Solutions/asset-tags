"""Reprint list (issue #36)."""

from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.tagops import record_printed, reprint_queue
from tests.test_app import login


def _tag(conn, **fields):
    conn.execute("INSERT INTO clients(name, label_name, active, updated_at) VALUES('Acme Ltd', 'Acme', 1, '')")
    cid = conn.execute("SELECT MAX(id) FROM clients").fetchone()[0]
    return cid, db.issue_tags(conn, 1, cid, "standard", "admin", dict({"device_name": "FW01"}, **fields))[0]


def _web():
    c = TestClient(app)
    login(c)
    return c


def test_reporting_a_damaged_label_lists_it_and_printing_clears_it(conn):
    _, code = _tag(conn)
    web = _web()
    web.post(f"/tags/{code}/label-problem", data={"problem": "damaged", "note": "peeling"})
    page = web.get("/reprint").text
    assert code in page and "Label damaged" in page and "peeling" in page
    assert f"/print?codes={code}" in page  # no printer set up in tests: the print page
    assert "Need a new label" in web.get("/").text
    record_printed(conn, [db.get_tag(conn, code)], "admin", "test")
    assert reprint_queue(conn) == [] and db.get_tag(conn, code)["label_problem"] == ""


def test_changed_client_name_puts_printed_tag_on_the_list(conn):
    cid, code = _tag(conn)
    record_printed(conn, [db.get_tag(conn, code)], "admin", "test")
    assert reprint_queue(conn) == []
    conn.execute("UPDATE clients SET label_name='Acme Group' WHERE id=?", (cid,))
    assert [t["code"] for t in reprint_queue(conn)] == [code]


def test_retired_tags_are_never_listed(conn):
    _, code = _tag(conn)
    conn.execute("UPDATE tags SET label_problem='missing' WHERE code=?", (code,))
    conn.execute("UPDATE tags SET status='retired' WHERE code=?", (code,))
    assert reprint_queue(conn) == []
    web = _web()
    web.post(f"/tags/{code}/label-problem", data={"problem": "damaged"})
    assert db.get_tag(conn, code)["label_problem"] == "missing"  # unchanged: retired


def test_cancel_report_and_history(conn):
    _, code = _tag(conn)
    web = _web()
    web.post(f"/tags/{code}/label-problem", data={"problem": "faded"})
    web.post(f"/tags/{code}/label-problem", data={"problem": ""})
    assert db.get_tag(conn, code)["label_problem"] == ""
    actions = [r["action"] for r in conn.execute("SELECT action FROM events WHERE code=?", (code,))]
    assert "label reported" in actions and "label report cleared" in actions


def test_unknown_problem_is_ignored_and_empty_list_message(conn):
    _, code = _tag(conn)
    web = _web()
    web.post(f"/tags/{code}/label-problem", data={"problem": "<script>"})
    assert db.get_tag(conn, code)["label_problem"] == ""
    assert "Nothing to reprint" in web.get("/reprint").text
