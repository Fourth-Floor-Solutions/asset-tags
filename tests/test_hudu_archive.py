"""Retiring tags whose Hudu asset is archived (issue #21)."""

import pytest
from fastapi.testclient import TestClient

from app import db, hudu, hudu_archive
from app.main import app
from tests.test_app import login


def _tag(conn, asset_id):
    conn.execute("INSERT INTO clients(name, label_name, active, updated_at) VALUES('Acme', 'Acme', 1, '')")
    cid = conn.execute("SELECT MAX(id) FROM clients").fetchone()[0]
    return db.issue_tags(conn, 1, cid, "standard", "admin", {"hudu_asset_id": asset_id, "device_name": "FW01"})[0]


def _events(conn, code):
    return [(e["actor"], e["action"], e["detail"]) for e in conn.execute("SELECT * FROM events WHERE code=?", (code,))]


def test_archived_asset_retires_tag_without_writing_to_hudu(fake, conn):
    code = _tag(conn, 10)
    fake.assets[10]["archived"] = True
    s = hudu_archive.run(conn)
    tag = db.get_tag(conn, code)
    assert s["retired"] == [code] and s["checked"] == 1 and not s["error"]
    assert tag["status"] == "retired" and tag["hudu_state"] == "archived"
    assert (hudu_archive.ACTOR, "retired", "Archived in Hudu (asset 10)") in _events(conn, code)
    assert not any(c[0] == "PUT" for c in fake.calls)  # the archived asset keeps its Asset Tag field
    # Retired by the check, so normal Hudu syncing won't try to clear the field later.
    from app.tagops import hudu_needs_sync

    assert not hudu_needs_sync(tag)
    assert hudu_archive.last_run(conn)["retired"] == [code]


def test_active_asset_leaves_tag_alone(fake, conn):
    code = _tag(conn, 10)
    s = hudu_archive.run(conn)
    assert s["retired"] == [] and db.get_tag(conn, code)["status"] == "active"
    assert db.get_tag(conn, code)["hudu_state"] == ""


def test_missing_asset_is_flagged_not_changed(fake, conn):
    code = _tag(conn, 99)  # not in the fake Hudu
    s = hudu_archive.run(conn)
    assert s["missing"] == [code]
    tag = db.get_tag(conn, code)
    assert tag["status"] == "active" and tag["hudu_state"] == "missing"
    assert [t["code"] for t in hudu_archive.for_review(conn)] == [code]
    hudu_archive.run(conn)  # flagged again, but logged only once
    assert sum(1 for e in _events(conn, code) if "not found" in (e[2] or "")) == 1


def test_404_counts_as_missing(fake, monkeypatch):
    def gone(asset_id):
        raise hudu.HuduError("Hudu returned HTTP 404.", 404)

    monkeypatch.setattr(hudu, "get_asset", gone)
    assert hudu.asset_state(5) == "missing"


def test_unarchived_asset_is_listed_for_review_not_restored(fake, conn):
    code = _tag(conn, 10)
    fake.assets[10]["archived"] = True
    hudu_archive.run(conn)
    fake.assets[10]["archived"] = False
    s = hudu_archive.run(conn)
    tag = db.get_tag(conn, code)
    assert s["unarchived"] == [code] and tag["status"] == "retired" and tag["hudu_state"] == "unarchived"
    c = TestClient(app)
    login(c)
    c.post(f"/tags/{code}/keep-retired")
    assert db.get_tag(conn, code)["hudu_state"] == "" and not hudu_archive.for_review(conn)
    hudu_archive.run(conn)  # no longer checked: retired by a person's decision
    assert db.get_tag(conn, code)["hudu_state"] == ""


def test_restoring_or_relinking_clears_the_review(fake, conn):
    code = _tag(conn, 99)
    hudu_archive.run(conn)
    c = TestClient(app)
    login(c)
    c.post(f"/tags/{code}/status", data={"status": "retired", "reason": "device gone"})
    assert db.get_tag(conn, code)["hudu_state"] == ""


def test_hudu_error_stops_the_run(fake, conn, monkeypatch):
    _tag(conn, 10)
    _tag(conn, 11)

    def down(asset_id):
        raise hudu.HuduError("Couldn't reach Hudu: timed out")

    monkeypatch.setattr(hudu, "get_asset", down)
    s = hudu_archive.run(conn)
    assert s["checked"] == 0 and s["of"] == 2 and "timed out" in s["error"]


def test_only_one_run_at_a_time(fake, conn):
    hudu_archive._running.acquire()
    try:
        assert "skipped" in hudu_archive.run(conn)
    finally:
        hudu_archive._running.release()


def test_admin_runs_check_and_sees_result(fake, conn):
    code = _tag(conn, 10)
    fake.assets[10]["archived"] = True
    c = TestClient(app)
    login(c)
    r = c.post("/hudu/archive-check", follow_redirects=True)
    assert "1 retired" in r.text and "Devices archived in Hudu" in r.text and f"/tags/{code}" in r.text


@pytest.mark.parametrize(("raw", "minutes"), [("60", 60), ("0", 0), ("15", 15), ("x", 60)])
def test_interval_from_environment(monkeypatch, raw, minutes):
    monkeypatch.setenv("HUDU_ARCHIVE_CHECK_MINUTES", raw)
    assert hudu_archive.interval_minutes() == minutes


def test_timer_off_when_interval_is_zero(monkeypatch):
    monkeypatch.setenv("HUDU_ARCHIVE_CHECK_MINUTES", "0")
    assert hudu_archive.start() is None
