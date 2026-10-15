"""Tag coverage (issue #33), and Hudu features being off while Hudu isn't set up."""

import pytest
from fastapi.testclient import TestClient

from app import coverage, db, settings
from app.main import app
from tests.test_app import login


def _client(conn, hudu_id=3):
    conn.execute(
        "INSERT INTO clients(hudu_id, name, label_name, active, updated_at) VALUES(?, 'Acme', 'Acme', 1, '')",
        (hudu_id,),
    )
    return conn.execute("SELECT * FROM clients WHERE hudu_id = ?", (hudu_id,)).fetchone()


def _admin():
    c = TestClient(app)
    login(c)
    return c


def test_refresh_and_counts(fake, conn):
    c = _client(conn)
    db.issue_tags(conn, 1, c["id"], "standard", "admin", {"hudu_asset_id": 10})  # FW01 tagged
    db.issue_tags(conn, 1, c["id"], "standard", "admin", {"device_name": "Loose"})  # not linked
    assert coverage.refresh(conn, c) == ""
    cov = coverage.for_client(conn, c)
    assert cov["assets"] == 2 and cov["tagged"] == 1 and cov["pct"] == 50
    assert [a["name"] for a in cov["untagged"]] == ["NAS01"]
    assert len(cov["unlinked"]) == 1
    assert not any(m == "PUT" for m, _ in fake.calls)  # read-only


def test_retired_tags_dont_count(fake, conn):
    c = _client(conn)
    code = db.issue_tags(conn, 1, c["id"], "standard", "admin", {"hudu_asset_id": 10})[0]
    conn.execute("UPDATE tags SET status='retired' WHERE code=?", (code,))
    coverage.refresh(conn, c)
    assert coverage.for_client(conn, c)["tagged"] == 0


def test_excluded_layouts_are_left_out(fake, conn):
    c = _client(conn)
    coverage.refresh(conn, c)
    assert {lay["id"] for lay in coverage.layouts_seen(conn)} == {5, 7}
    cov = coverage.for_client(conn, c, exclude={7})  # NAS layout
    assert cov["assets"] == 1 and [a["name"] for a in cov["untagged"]] == ["FW01"]


def test_archived_assets_are_not_counted(fake, conn):
    fake.assets[11]["archived"] = True
    c = _client(conn)
    coverage.refresh(conn, c)
    assert coverage.for_client(conn, c)["assets"] == 1


def test_failed_refresh_keeps_previous_result(fake, conn, monkeypatch):
    from app import hudu

    c = _client(conn)
    coverage.refresh(conn, c)

    def down(company_id):
        raise hudu.HuduError("Couldn't reach Hudu: timed out")

    monkeypatch.setattr(hudu, "company_assets", down)
    assert "timed out" in coverage.refresh(conn, c)
    cov = coverage.for_client(conn, c)
    assert cov["assets"] == 2 and "timed out" in cov["error"]


def test_pages_and_tag_this_device(fake, conn):
    c = _client(conn)
    web = _admin()
    assert "Not fetched yet" in web.get("/coverage").text
    web.post(f"/coverage/{c['id']}/refresh")
    r = web.get(f"/coverage/{c['id']}")
    assert r.status_code == 200 and "NAS01" in r.text and "Tag this device" in r.text
    assert "50%" not in web.get("/coverage").text  # nothing tagged yet: 0%
    form = web.get(f"/tags/new?client_id={c['id']}&hudu_asset_id=11&device_name=NAS01&serial=SN-123").text
    assert 'value="NAS01"' in form and 'value="SN-123"' in form and 'value="11"' in form
    csv_text = web.get(f"/coverage/{c['id']}/untagged.csv").text
    assert csv_text.startswith("﻿Device,Type") and "NAS01" in csv_text


def test_csv_cells_cant_become_formulas(fake, conn):
    fake.assets[11]["name"] = "=HYPERLINK(1)"
    c = _client(conn)
    web = _admin()
    web.post(f"/coverage/{c['id']}/refresh")
    assert "'=HYPERLINK(1)" in web.get(f"/coverage/{c['id']}/untagged.csv").text


def test_admin_chooses_counted_layouts(fake, conn):
    c = _client(conn)
    web = _admin()
    web.post(f"/coverage/{c['id']}/refresh")
    web.post("/coverage/layouts", data={"shown": ["5", "7"], "count": ["5"]})
    assert settings.get("hudu_coverage_exclude") == "7"
    web.post("/coverage/layouts", data={"shown": ["5", "7"], "count": ["5", "7"]})
    assert settings.get("hudu_coverage_exclude") == ""


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/coverage"),
        ("post", "/coverage/refresh"),
        ("get", "/api/hudu/assets?client_id=1"),
        ("post", "/clients/sync"),
        ("post", "/hudu/sync-all"),
        ("post", "/hudu/archive-check"),
    ],
)
def test_hudu_features_are_off_without_hudu(method, path):
    web = _admin()
    r = getattr(web, method)(path, follow_redirects=False)
    assert r.status_code == 404 and "set up" in r.text and "Hudu isn" in r.text


def test_hudu_ui_hidden_without_hudu():
    web = _admin()
    assert 'href="/coverage"' not in web.get("/").text
    page = web.get("/clients").text
    assert "Sync from Hudu" not in page and "Devices archived in Hudu" not in page


def test_editing_a_tag_with_hudu_off_keeps_its_link(conn):
    web = _admin()
    cid = conn.execute("SELECT id FROM clients LIMIT 1").fetchone()
    if not cid:
        conn.execute("INSERT INTO clients(name, label_name, active, updated_at) VALUES('X','X',1,'')")
        cid = conn.execute("SELECT id FROM clients LIMIT 1").fetchone()
    code = db.issue_tags(conn, 1, cid[0], "standard", "admin", {"hudu_asset_id": 77, "device_name": "A"})[0]
    web.post(f"/tags/{code}", data={"client_id": cid[0], "format": "standard", "device_name": "B"})
    tag = db.get_tag(conn, code)
    assert tag["device_name"] == "B" and tag["hudu_asset_id"] == 77
