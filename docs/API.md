# TagDesk API

A small JSON API for other systems, such as an RMM, a PSA automation or a
script, to look up and update tags.

## Keys

An admin creates a key in **Settings → API keys**, giving it a name and
choosing what it can do:

- **Read only:** look up tags.
- **Read and write:** also change a tag's device details and Hudu link,
  and retire tags.

The key is shown once when it's created. TagDesk keeps only a hash of it,
so a lost key can't be recovered: revoke it and create a new one. Revoking
takes effect immediately. Settings shows when each key was last used.

Send the key in a header with every request:

```
Authorization: Bearer td_…
```

Each key can make up to 120 requests a minute; past that, requests get
HTTP 429. Changes made through the API appear in the tag's history as
**API: *key name***.

## Endpoints

All responses are JSON. Errors look like `{"error": "…"}` with an HTTP
status of 400, 401, 403, 404, 409 or 429.

### Look up one tag

```
GET /api/v1/tags/{code}
```

Codes are case-insensitive. O, I and L are read as 0, 1 and 1.

### Find tags

```
GET /api/v1/tags?serial=&client=&status=&hudu_asset_id=
```

| Parameter | Matches |
|---|---|
| `serial` | Serial number, ignoring case, spaces and dashes |
| `client` | TagDesk client id |
| `status` | `active` (assigned to a device), `unassigned` or `retired` |
| `hudu_asset_id` | The linked Hudu asset |

Returns `{"tags": [...]}`, up to 500 tags.

### Update a tag (read and write keys)

```
PATCH /api/v1/tags/{code}
Content-Type: application/json

{"device_name": "SITE01-NAS01", "model": "DS1522+", "serial": "2280PDN001"}
```

Editable fields:
- `device_name`, `model`, `serial`, `notes`
- `hudu_asset_id` and `hudu_asset_url`: link the tag to a Hudu asset, or
  set `hudu_asset_id` to `null` to unlink it. Linking needs Hudu to be set
  up in TagDesk.

Sending any other field returns 400. A tag with a device name, serial or
Hudu link becomes **Assigned**. If writing to Hudu is switched on, the tag
code is written to the linked asset, as it is for changes made in the app.
Retired tags can't be changed (409).

### Retire a tag (read and write keys)

```
POST /api/v1/tags/{code}/retire
Content-Type: application/json

{"reason": "Disposed"}
```

### A tag

```json
{
  "code": "7KQ3M",
  "url": "https://assets.example.com/7KQ3M",
  "status": "active",
  "format": "standard",
  "format_name": "Standard",
  "client": {"id": 3, "name": "Acme Ltd"},
  "device_name": "ACME-FW01",
  "model": "60F",
  "serial": "FGT60FTK00000000",
  "notes": "",
  "hudu_asset_id": 1234,
  "hudu_asset_url": "https://hudu.example.com/a/acme-fw01",
  "printed_count": 1,
  "printed_at": "2026-10-16T09:00:00+00:00",
  "created_at": "2026-10-15T14:00:00+00:00",
  "updated_at": "2026-10-16T09:05:00+00:00"
}
```

## Example

Find the tag on a device by its serial number:

```sh
curl -s -H "Authorization: Bearer $TAGDESK_KEY" \
  "https://assets.example.com/api/v1/tags?serial=FGT60FTK00000000"
```
