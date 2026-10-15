import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="asset-tags-test-")
os.environ["ADMIN_PASSWORD"] = "test-password-123"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ.pop("HUDU_URL", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


@pytest.fixture(autouse=True)
def _no_real_snmp(monkeypatch):
    """Tests never talk to a real printer: SNMP reads fail fast unless a test mocks them."""
    from app import printer

    def offline(host, oid, *a, **k):
        raise printer.PrinterError("no network in tests")
    monkeypatch.setattr(printer, "_snmp_get", offline)
