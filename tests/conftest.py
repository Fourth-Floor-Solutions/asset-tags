import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="tagdesk-test-")
os.environ["ADMIN_PASSWORD"] = "test-password-123"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
for _var in (
    "HUDU_URL",
    "HUDU_API_KEY",
    "ENTRA_TENANT_ID",
    "ENTRA_CLIENT_ID",
    "ENTRA_CLIENT_SECRET",
    "SOURCE_URL",
    "TAGDESK_SECRET_KEY",
    "PRINTER_HOST",
):
    os.environ.pop(_var, None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_app_state(tmp_path, monkeypatch):
    """Every test gets its own empty database and data folder, and no state left over from other tests."""
    from app import auth, db, hudu, labels
    from app.routes import login, public

    # Cheap password hashing in tests: each test creates the admin account afresh.
    monkeypatch.setattr(auth, "ITERATIONS", 1000)
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "tagdesk.db"))
    monkeypatch.setattr(db, "LEGACY_DB_PATH", str(tmp_path / "asset-tags.db"))
    db.init()
    auth.seed_admin()
    labels.apply_overrides({})
    login._login_fails.clear()
    public._scan_hits.clear()
    hudu._layout_cache.clear()
    monkeypatch.setattr(auth, "_oauth", None)
    monkeypatch.setattr(auth, "_oauth_for", None)
    yield
    labels.apply_overrides({})


@pytest.fixture(autouse=True)
def _no_real_snmp(monkeypatch):
    """Tests never talk to a real printer: SNMP reads fail fast unless a test mocks them."""
    from app import printer

    if not hasattr(printer, "_snmp_get_real"):
        printer._snmp_get_real = printer._snmp_get  # for tests that decode a fake SNMP reply

    def offline(host, oid, *a, **k):
        raise printer.PrinterError("no network in tests")

    monkeypatch.setattr(printer, "_snmp_get", offline)


@pytest.fixture
def conn():
    """A database connection for the test, closed afterwards."""
    from app import db

    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def fake(monkeypatch):
    """Hudu set up and answered by tests.test_hudu.FakeHudu instead of the network."""
    from app import hudu, settings
    from tests.test_hudu import FakeHudu

    f = FakeHudu()
    settings.save({"hudu_url": "https://hudu.example.com"})
    settings.save_secret("hudu_api_key", "test-key")
    monkeypatch.setattr(hudu, "_request", f.request)
    monkeypatch.setattr(hudu, "PAUSE", 0)
    return f
