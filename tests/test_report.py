"""Report a problem from the public scan page (issue #35)."""

import html
from urllib.parse import parse_qs, unquote, urlparse

from fastapi.testclient import TestClient

from app import db, settings
from app.main import app


def _tag(conn, **fields):
    conn.execute("INSERT INTO clients(name, label_name, active, updated_at) VALUES('Acme Ltd', 'Acme', 1, '')")
    cid = conn.execute("SELECT MAX(id) FROM clients").fetchone()[0]
    return db.issue_tags(conn, 1, cid, "standard", "admin", dict({"device_name": "FW01"}, **fields))[0]


def _report_link(page: str) -> str:
    start = page.index('href="mailto:')
    return html.unescape(page[start + 6 : page.index('"', start + 6)])


def test_report_button_prefills_email(conn):
    settings.save({"support_email": "help@example.com", "support_phone": "+44 (0)20 0000 0000"})
    code = _tag(conn)
    page = TestClient(app).get(f"/{code}").text
    assert "Report a problem" in page and "Call support" in page
    link = urlparse(_report_link(page))
    q = parse_qs(link.query)
    assert link.path == "help@example.com"
    assert q["subject"] == [f"Problem with {code} (FW01, Acme Ltd)"]
    body = q["body"][0]
    assert f"Tag: {code}" in body and "Client: Acme Ltd" in body and f"/{code}" in body


def test_subject_template_and_button_text_from_settings(conn):
    settings.save(
        {
            "support_email": "help@example.com",
            "report_button": "Something wrong?",
            "report_subject": "[{client}] {code}",
        }
    )
    code = _tag(conn)
    page = TestClient(app).get(f"/{code}").text
    assert "Something wrong?" in page
    assert parse_qs(urlparse(_report_link(page)).query)["subject"] == [f"[Acme Ltd] {code}"]


def test_no_report_button_without_support_email_or_for_retired(conn):
    code = _tag(conn)
    assert "Report a problem" not in TestClient(app).get(f"/{code}").text
    settings.save({"support_email": "help@example.com"})
    conn.execute("UPDATE tags SET status='retired' WHERE code=?", (code,))
    page = TestClient(app).get(f"/{code}").text
    assert "Report a problem" not in page and "no longer manages this device" in page


def test_unknown_code_has_no_report_button():
    settings.save({"support_email": "help@example.com"})
    page = TestClient(app).get("/ZZZZZ").text
    assert "Report a problem" not in page


def test_device_name_is_encoded_not_injected(conn):
    settings.save({"support_email": "help@example.com"})
    code = _tag(conn, device_name='X"><script>alert(1)</script>&cc=evil@example.com')
    page = TestClient(app).get(f"/{code}").text
    assert "<script>alert(1)" not in page
    link = _report_link(page)
    assert "cc=" not in unquote(link.split("?", 1)[1].split("&body=")[0]).split("&")[0]
    assert set(parse_qs(urlparse(link).query)) == {"subject", "body"}
