"""Add the "Asset Tag" Text field to Hudu asset layouts.

Run inside the container:  python -m scripts.add_asset_tag_field 5 14 ... [--apply]

Hudu's layout PUT takes the full fields array (omitted fields are destroyed),
so every existing field is re-sent with its id. Each layout is backed up to
/data/hudu-backups first and checked afterwards: all previous field ids must
still be present, plus the new field.
"""
import json
import os
import sys
import time
from datetime import datetime

from app import hudu

HINT = "Asset tag code. Set by the Asset Tags app ({url}); don't edit by hand.".format(
    url=os.environ.get("PUBLIC_BASE_URL", "").replace("https://", "").replace("http://", "") or "asset tags")


def layout(lid):
    data = hudu._get(f"/asset_layouts/{lid}", {})
    return data.get("asset_layout", data)


def main(args):
    apply = "--apply" in args
    ids = [int(a) for a in args if a.isdigit()]
    backup_dir = os.path.join(os.environ.get("DATA_DIR", "/data"), "hudu-backups")
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    for lid in ids:
        lay = layout(lid)
        fields = [f for f in lay["fields"] if not f.get("is_destroyed")]
        if any(f["label"].strip().lower() == hudu.tag_label().lower() for f in fields):
            print(f"{lid} {lay['name']}: already has {hudu.tag_label()}")
            continue
        path = os.path.join(backup_dir, f"layout-{lid}-{ts}.json")
        with open(path, "w") as f:
            json.dump(lay, f, indent=1)
        pos = max([f.get("position") or 0 for f in fields] + [0]) + 1
        new = {"label": hudu.tag_label(), "field_type": "Text", "show_in_list": True,
               "required": False, "hint": HINT, "position": pos}
        print(f"{lid} {lay['name']}: {len(fields)} fields -> add '{hudu.tag_label()}' at position {pos}"
              f" (backup {path})" + ("" if apply else "  [dry run]"))
        if not apply:
            continue
        hudu._request("PUT", f"/asset_layouts/{lid}", body={"asset_layout": {"fields": fields + [new]}})
        time.sleep(1)
        after = [f for f in layout(lid)["fields"] if not f.get("is_destroyed")]
        missing = {f["id"] for f in fields} - {f["id"] for f in after}
        added = [f for f in after if f["label"] == hudu.tag_label()]
        if missing or not added:
            print(f"   !! CHECK FAILED: missing field ids {sorted(missing)}, added={bool(added)}. Stopping.")
            sys.exit(1)
        print(f"   ok: {len(after)} fields, all previous ids kept, new field id {added[0]['id']}")
        time.sleep(1)


if __name__ == "__main__":
    main(sys.argv[1:])
