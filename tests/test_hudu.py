"""Hudu client, against a small in-memory fake of the parts of the Hudu API we use."""

import copy
import json

import pytest

from app import hudu, settings


class FakeHudu:
    """Assets keyed by id; PUT replaces custom fields (like Hudu); records every call."""

    def __init__(self):
        self.layouts = {5: [{"label": "Model"}, {"label": "Location"}, {"label": "Asset Tag"}], 7: [{"label": "Model"}]}
        self.assets = {
            10: {
                "id": 10,
                "name": "FW01",
                "company_id": 3,
                "asset_layout_id": 5,
                "asset_type": "Network",
                "url": "/a/fw01",
                "primary_serial": None,
                "primary_model": None,
                "fields": [
                    {"label": "Model", "value": "60F"},
                    {"label": "Location", "value": json.dumps([{"id": 56, "name": "Office"}])},
                ],
            },
            11: {
                "id": 11,
                "name": "NAS01",
                "company_id": 3,
                "asset_layout_id": 7,
                "asset_type": "NAS",
                "url": "/a/nas01",
                "fields": [{"label": "Serial Number", "value": "SN-123"}],
            },
        }
        self.companies = [{"id": i, "name": f"Client {i}"} for i in range(1, 131)]
        self.calls = []
        self.mangle_location = False

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path))
        if path == "/companies":
            page, size = params["page"], params["page_size"]
            return {"companies": self.companies[(page - 1) * size : page * size]}
        if path.startswith("/asset_layouts/"):
            lid = int(path.rsplit("/", 1)[1])
            return {"asset_layout": {"id": lid, "fields": self.layouts[lid]}}
        if path == "/assets" and method == "GET":
            if "id" in params:
                a = self.assets.get(params["id"])
                return {"assets": [copy.deepcopy(a)] if a else []}
            found = [a for a in self.assets.values() if a["company_id"] == params.get("company_id")]
            q = params.get("search")
            if q:  # Hudu's search only matches names
                found = [a for a in found if q.lower() in a["name"].lower()]
            return {"assets": copy.deepcopy(found)}
        if method == "PUT" and "/assets/" in path:
            aid = int(path.rsplit("/", 1)[1])
            fields = []
            for item in body["asset"]["custom_fields"]:
                for key, value in item.items():
                    label = next(
                        (
                            f["label"]
                            for f in self.layouts[self.assets[aid]["asset_layout_id"]]
                            if hudu.field_key(f["label"]) == key
                        ),
                        key,
                    )
                    if self.mangle_location and label == "Location":
                        value = json.dumps([{"id": 99, "name": "Elsewhere"}])
                    fields.append({"label": label, "value": value})
            self.assets[aid]["fields"] = fields  # full replace, like Hudu
            return {"asset": self.assets[aid]}
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def fake(monkeypatch):
    f = FakeHudu()
    settings.save({"hudu_url": "https://hudu.example.com"})
    settings.save_secret("hudu_api_key", "test-key")
    monkeypatch.setattr(hudu, "_request", f.request)
    monkeypatch.setattr(hudu, "PAUSE", 0)
    return f


def _fields(asset):
    return {f["label"]: f["value"] for f in asset["fields"]}


def test_list_companies_pages_through(fake):
    assert len(hudu.list_companies()) == 130
    assert sum(1 for c in fake.calls if c[1] == "/companies") == 2


def test_search_by_name_and_by_custom_field_serial(fake):
    assert [a["id"] for a in hudu.search_assets(3, "fw01")] == [10]
    found = hudu.search_assets(3, "SN-123")  # not in the name: falls back to scanning fields
    assert [a["id"] for a in found] == [11] and found[0]["serial"] == "SN-123"
    assert found[0]["url"] == "https://hudu.example.com/a/nas01"


def test_set_tag_writes_and_keeps_other_fields(fake):
    assert hudu.set_tag(10, "7KQ3M") == "written"
    f = _fields(fake.assets[10])
    assert f["Asset Tag"] == "7KQ3M" and f["Model"] == "60F" and "56" in f["Location"]
    assert hudu.set_tag(10, "7KQ3M") == "already up to date"


def test_set_tag_skips_layout_without_field(fake):
    assert hudu.set_tag(11, "7KQ3M").startswith("skipped")
    assert not any(c[0] == "PUT" for c in fake.calls)


def test_clear_only_if_it_still_holds_our_code(fake):
    hudu.set_tag(10, "OTHER")
    assert hudu.set_tag(10, "", only_if="7KQ3M").startswith("left alone")
    assert _fields(fake.assets[10])["Asset Tag"] == "OTHER"
    assert hudu.set_tag(10, "", only_if="OTHER") == "cleared"


def test_other_field_changing_is_detected_and_restored(fake):
    fake.mangle_location = True
    with pytest.raises(hudu.HuduError, match="location"):
        hudu.set_tag(10, "7KQ3M")
    fake.mangle_location = False
    # the restore ran: Asset Tag reset to its original (empty) value
    assert _fields(fake.assets[10]).get("Asset Tag", "") == ""


def test_linked_fields_compare_by_id():
    a = json.dumps([{"id": 56, "name": "Office"}])
    b = json.dumps([{"id": 56, "url": "/a/office", "name": "Office"}])
    assert hudu._norm(a) == hudu._norm(b) and hudu._norm(a) != hudu._norm(json.dumps([{"id": 57}]))


def test_not_configured_is_a_clear_error(monkeypatch):
    settings.save({"hudu_url": ""})
    with pytest.raises(hudu.HuduError, match="isn't set up"):
        hudu._request("GET", "/companies")
