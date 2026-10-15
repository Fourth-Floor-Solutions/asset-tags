import os
import stat

from fastapi.testclient import TestClient

from app import auth, db, hudu, secretbox, settings
from app.main import app
from tests.test_app import login


def _admin():
    from app import main
    main._login_fails.clear()
    c = TestClient(app)
    login(c)
    return c


def test_encrypt_roundtrip_and_key_file(monkeypatch):
    monkeypatch.delenv("TAGDESK_SECRET_KEY", raising=False)
    token = secretbox.encrypt("hunter2-api-key")
    assert token.startswith(secretbox.PREFIX) and "hunter2" not in token
    assert secretbox.decrypt(token) == "hunter2-api-key"
    mode = stat.S_IMODE(os.stat(os.path.join(db.DATA_DIR, secretbox.KEY_FILE)).st_mode)
    assert mode == 0o600


def test_wrong_key_is_reported_not_silent(monkeypatch):
    settings.save_secret("hudu_api_key", "abc123")
    from cryptography.fernet import Fernet
    monkeypatch.setenv("TAGDESK_SECRET_KEY", Fernet.generate_key().decode())
    info = settings.secret_info("hudu_api_key")
    assert info["status"] == "unreadable" and "Enter it again" in info["error"]
    monkeypatch.delenv("TAGDESK_SECRET_KEY")
    settings.save_secret("hudu_api_key", None)


def test_env_wins_and_copy_into_settings(monkeypatch):
    monkeypatch.setenv("HUDU_API_KEY", "from-env")
    assert settings.secret_info("hudu_api_key")["status"] == "env"
    c = _admin()
    assert "Copy into Settings" in c.get("/settings").text
    c.post("/settings/secrets/hudu_api_key/import")
    monkeypatch.delenv("HUDU_API_KEY")
    info = settings.secret_info("hudu_api_key")
    assert info == {"status": "set", "value": "from-env", "error": ""}
    settings.save_secret("hudu_api_key", None)


def test_secrets_write_only_and_kept_when_empty():
    c = _admin()
    base = {"org_name": "Test Org", "org_mark": "TO", "accent": "#5b8def", "hudu_url": "https://hudu.example.com"}
    c.post("/settings", data=dict(base, hudu_api_key="secret-one"))
    assert settings.secret("hudu_api_key") == "secret-one" and hudu.configured()
    page = c.get("/settings").text
    assert "secret-one" not in page and "saved, encrypted" in page
    c.post("/settings", data=dict(base, hudu_api_key=""))                 # empty keeps it
    assert settings.secret("hudu_api_key") == "secret-one"
    c.post("/settings", data=dict(base, clear_hudu_api_key="1"))         # explicit remove
    assert settings.secret("hudu_api_key") is None
    rows = [dict(r) for r in settings.recent_audit(50)]
    assert any(r["setting"] == "hudu_api_key" and r["change"] == "changed" for r in rows)
    assert not any("secret-one" in r["change"] for r in rows)
    settings.save({"hudu_url": ""})


def test_sso_from_settings_rebuilds_without_restart():
    c = _admin()
    base = {"org_name": "Test Org", "org_mark": "TO", "accent": "#5b8def"}
    guid = "11111111-2222-3333-4444-555555555555"
    c.post("/settings", data=dict(base, entra_tenant_id="example.onmicrosoft.com", entra_client_id=guid,
                                  entra_client_secret="s3cret"))
    assert auth.sso_enabled() and auth.oauth() is not None
    first = auth.oauth()
    c.post("/settings", data=dict(base, entra_client_secret="s3cret-2"))
    assert auth.oauth() is not first                                      # rebuilt after the change
    assert c.post("/settings", data=dict(base, entra_client_id="not-a-guid")).status_code == 400
    c.post("/settings", data=dict(base, entra_tenant_id="", entra_client_id="", clear_entra_client_secret="1"))
    assert not auth.sso_enabled()


def test_missing_fields_keep_their_values():
    c = _admin()
    settings.save({"support_phone": "020 7946 0001"})
    c.post("/settings", data={"org_name": "Test Org", "org_mark": "TO", "accent": "#5b8def"})
    assert settings.get("support_phone") == "020 7946 0001"


def test_secret_expiry_warning():
    from datetime import date, timedelta
    c = _admin()
    settings.save({"entra_secret_expires": (date.today() + timedelta(days=10)).isoformat()})
    assert "expires in 10 days" in c.get("/").text
    settings.save({"entra_secret_expires": (date.today() + timedelta(days=200)).isoformat()})
    assert "expires in" not in c.get("/").text
    settings.save({"entra_secret_expires": ""})
