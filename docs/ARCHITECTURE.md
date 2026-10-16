# How TagDesk works

A short tour for contributors. TagDesk is one FastAPI process with a SQLite
database, server-rendered Jinja templates and a little plain JavaScript.

## Layout

| Path | What's there |
|---|---|
| `app/main.py` | Builds the app: security headers, the cross-site post check, loading the signed-in user, sessions, error pages, and the routers. |
| `app/routes/` | One module per area: `tags` (list, issuing, one tag, Hudu writes), `printing` (print sheet, direct printing, printers, cassettes), `settings`, `clients`, `users`, `login`, `public` (favicon, `/version`, `/healthz`, What's new, and the scan page). |
| `app/web.py` | `page()` renders a template with everything every page needs; `flash`, `go`, and the link checks (`safe_next`, `hudu_link`). |
| `app/tagops.py` | Tag logic used by several areas: label text, reprint checks, Hudu writes, print records. |
| `app/labels.py` | Label formats and layouts; drawn as SVG and as 360 dpi bitmaps. |
| `app/printer.py` | Brother raster jobs, status over SNMP, routing labels to printers. |
| `app/usage.py` | Tape tracking per cassette. |
| `app/hudu.py` | The Hudu API client. |
| `app/settings.py`, `app/secretbox.py` | Settings with defaults and `.env` overrides; encrypted secrets. |
| `app/auth.py` | Local accounts and Microsoft (Entra ID) sign-in. |
| `app/db.py`, `app/codes.py` | SQLite schema and migrations; tag codes. |
| `app/log.py` | The application log. |
| `app/apikeys.py`, `app/routes/api.py` | API keys and the JSON API under `/api/v1` ([API.md](API.md)). |
| `app/routes/site.py`, `static/site.js`, `static/jsQR.js` | Tag a site: scanning in the browser with jsQR, saving through small JSON endpoints. |
| `app/coverage.py`, `app/hudu_archive.py`, `app/backup.py` | Tag coverage, the Hudu archive check, and backups. |

The scan route `/{raw}` matches any single path segment, so the `public`
router is included last.

## Tag codes

Five characters of Crockford Base32 (`app/codes.py`): no I, L, O or U, and
O, I and L typed by mistake are read as 0, 1 and 1. That's 32⁵ (about 33
million) codes. Codes are handed out in sequence from a random starting
point in the lower half of the space, so they don't start at `00000` and
there's room to count upwards for decades. `db.issue_tags` takes the next
numbers inside one `BEGIN IMMEDIATE` transaction, so two people issuing at
once never get the same code.

## Labels: one layout, two renderings

`labels.layout()` describes a label as a list of elements in millimetres:
text runs, rectangles and the QR code, each with a fill of `ink` or `tape`.
The same list is drawn two ways:

- `render()` builds an **SVG** for the on-screen preview and for printing
  from the browser. Text that might be too long (client names) carries
  `data-maxw`, and `static/app.js` squeezes it to fit.
- `raster()` draws a **1-bit bitmap at 360 dpi** (the printer's
  resolution) for direct printing, squeezing long text the same way and
  using the same fonts from `app/fonts`.

Because both come from one layout, what's on screen is what gets printed.
Formats (Standard 18 mm, Slim 12 mm, Micro 6 mm, Loan 18 mm red) live in
`labels.FORMATS`. Admins can switch formats off and change the tape colour
and description in Settings; sizes are fixed because each layout is drawn
for its tape width. Those changes are applied to `FORMATS` in place, which
assumes a single worker process (how the Docker image runs).

## Printing to a Brother PT-P950NW

Tested on the PT-P950NW only; other PT-P900-series printers use the same
command set. The printer must be reachable from the server.

- **Status** comes over SNMP: the printer's 32-byte status block (tape
  width and colour, errors, busy). Port 9100 doesn't answer status requests
  on this model's network interface. `printer.py` has a tiny SNMP v2c client (community `public`),
  so there's no extra dependency.
- **Jobs** are Brother raster commands sent raw to TCP 9100. Each column of
  the label becomes one 70-byte raster line (560 head pins). Columns are
  sent right to left: left to right came out mirrored. The print area
  starts after the right-hand margin pins (`560 − left − area` from the
  raster reference's tape table); counting from the left margin printed
  about 1 mm low. A per-printer **nudge** (in dots) fine-tunes the vertical
  position.
- **Cutting:** each label is its own page. Normally every page is fully
  cut. Chain printing is always off, because it left the last label stuck
  in the printer. A batch can also come out as one strip, two ways.
  **One strip, cut them yourself** joins the labels into one long page
  (`join_strip`) with dashed lines between them, under the printer's 1 m
  page limit. **One strip, pre-cut** uses half cuts: each label is cut
  through but the backing isn't, so the strip stays in one piece and labels peel off one by one,
  with a full cut after the last. On the PT-P950NW this only works with
  auto cut turned off for the job; with auto cut on, its "cut every n"
  setting full-cut in the wrong places.
- **Routing:** with several printers, each label goes to the first printer
  (default first) that has the right tape loaded. Labels no printer can
  take are reported back as waiting.
- One lock per printer address stops two jobs interleaving.

## Tape tracking

Each time the app talks to a printer it reads the printer's lifetime
print-length counter over SNMP and charges the change since the last read
to the cassette that was loaded then (`usage.track`). Cassettes are told
apart by tape width and colour, so swapping between them keeps both
totals. The Printer page shows roughly how much is left and warns when it's
low. Users can say a new cassette was fitted or correct the amount used.

## Settings

`settings.get_all()` returns every setting, resolved in this order:

1. **`ENV_WINS`**: if the environment variable is set, it's used and the
   setting can't be edited in the app (Hudu API key, Microsoft sign-in,
   source link), so existing `.env`-based installs keep working.
2. A value **saved** in the database from the Settings page.
3. **`ENV_FALLBACK`**: an environment variable used until the setting is
   first saved.
4. The built-in **default**.

Secrets (the Hudu API key and the Microsoft client secret) are encrypted
with Fernet (`secretbox.py`). The key is in `TAGDESK_SECRET_KEY` or
`secret.key` in the data folder, never in the database, and a secret is
never shown again after it's saved. Every change is written to the `audit`
table and the log.

Within a request, settings are read from the database once and cached (a
`ContextVar` set by the middleware); saving clears the cache. Outside a
request, every read goes to the database.

## Hudu

Optional. Clients are synced from Hudu companies. On a tag's page, the
asset picker searches the client's Hudu assets. Hudu's `search` only
matches names, so when that finds nothing the client's assets are paged
through and matched on serial and model as well.

When writing is on, the tag's code is written to the linked asset's
**Asset Tag** field (the field name is a setting), and cleared again when
the tag is retired or linked to another asset (only if the asset still
holds this tag's code). Hudu's asset update replaces *all* custom fields,
so `hudu.set_tag` re-sends every field, reads the asset back, and restores
the previous values if anything else changed. Linked fields are compared
by id. Calls are spaced out, because bursts of requests trip Hudu's rate
limiting.

## Sign-in and security

- Local accounts: PBKDF2-SHA256 passwords. Failed sign-ins are limited per
  IP address, and unknown usernames take as long to check as wrong
  passwords.
- Microsoft sign-in (Entra ID, OpenID Connect via Authlib): single tenant
  only. Multi-tenant values are refused, and the token's tenant must match.
  Users are matched by email address; a user who isn't on the Users page
  can't sign in (unless their domain is set to be added automatically).
- Sessions are signed cookies (12 hours).
- Every response gets a Content-Security-Policy and the other security
  headers. Cross-site form posts are refused (Origin/Referer check).
  Redirects after sign-in stay on this site. Hudu links must point at the
  configured Hudu server.
- The public scan page shows only the owner, the device name and how to
  get support, and lookups are rate-limited per IP address.
- Only the reverse proxy given in `FORWARDED_ALLOW_IPS` may pass on the
  visitor's IP address.

See [SECURITY.md](../SECURITY.md) for reporting a vulnerability.

## Database

SQLite in `DATA_DIR`, autocommit, with a new connection per use
(`with db.connection() as conn:`). The schema is created by `db.init()`
at startup. New columns are added by `_add_columns`, which adds any that
are missing, so upgrades need no separate migration step. The `events`
table is each tag's history; `audit` is the log of setting changes.
