import pytest
from fastapi.testclient import TestClient

from app import main, settings
from app.main import app, safe_next
from tests.test_app import internal_client_id, login


def _admin():
    main._login_fails.clear()
    c = TestClient(app)
    login(c)
    return c


@pytest.mark.parametrize(
    "bad",
    [
        "//evil.example",
        "/\\evil.example",
        "https://evil.example",
        "javascript:alert(1)",
        "/\\/evil.example",
        "/ok\\..\\x",
        "/x\r\nLocation: //evil",
    ],
)
def test_safe_next_rejects_other_sites(bad):
    assert safe_next(bad) == "/"


def test_safe_next_keeps_local_paths():
    assert safe_next("/tags/7KQ3M") == "/tags/7KQ3M"


def test_login_and_sso_redirects_are_safe():
    c = TestClient(app)
    main._login_fails.clear()
    r = c.post(
        "/login",
        data={"username": "admin", "password": "test-password-123", "next": "/\\evil.example"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


def test_hudu_link_only_on_configured_host():
    c = _admin()
    settings.save({"hudu_url": "https://hudu.example.com"})
    cid = internal_client_id(c)
    for url, kept in (
        ("javascript:alert(1)", False),
        ("https://evil.example/a/x", False),
        ("https://hudu.example.com/a/asset-1", True),
    ):
        r = c.post(
            "/tags/new",
            data={
                "client_id": cid,
                "format": "standard",
                "device_name": "D",
                "hudu_asset_id": "5",
                "hudu_asset_url": url,
            },
            follow_redirects=False,
        )
        code = r.headers["location"].rsplit("/", 1)[1]
        page = c.get(f"/tags/{code}").text
        assert (url in page) is kept
    settings.save({"hudu_url": ""})


def test_cross_site_post_is_refused():
    c = _admin()
    r = c.post("/settings", data={"org_name": "Hijacked"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert settings.get("org_name") != "Hijacked"
    ok = c.post("/account/name", data={"name": "Sam"}, headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert ok.status_code == 303


def test_security_headers():
    h = TestClient(app).get("/login").headers
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"


def test_multi_tenant_refused():
    c = _admin()
    base = {"org_name": "Test Org", "org_mark": "TO", "accent": "#5b8def"}
    r = c.post(
        "/settings", data=dict(base, entra_tenant_id="common", entra_client_id="11111111-2222-3333-4444-555555555555")
    )
    assert r.status_code == 400 and "common" in r.text


def test_hudu_address_change_needs_key_again():
    c = _admin()
    base = {"org_name": "Test Org", "org_mark": "TO", "accent": "#5b8def"}
    c.post("/settings", data=dict(base, hudu_url="https://hudu.example.com", hudu_api_key="k1"))
    r = c.post("/settings", data=dict(base, hudu_url="https://other.example.com"))
    assert r.status_code == 400 and "enter the API key again" in r.text
    assert settings.get("hudu_url") == "https://hudu.example.com"
    c.post("/settings", data=dict(base, hudu_url="", clear_hudu_api_key="1"))


def test_public_scan_limit():
    main._scan_hits.clear()
    c = TestClient(app)
    codes = [c.get(f"/ZZZZ{x}").status_code for x in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" * 2]
    assert codes.count(404) == main.SCAN_MAX and codes[-1] == 429
    main._scan_hits.clear()


def test_sign_out_is_post_only():
    c = _admin()
    assert c.get("/logout").status_code in (404, 405)
    assert c.post("/logout", follow_redirects=False).status_code == 303
