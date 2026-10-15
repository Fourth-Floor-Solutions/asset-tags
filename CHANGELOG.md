# Changelog

What changed in TagDesk, newest first. Everything that changed on the same
day is listed under that one date, with any versions released that day in
the heading. Versions follow [semantic versioning](https://semver.org).

## 15 October 2026 · v0.1.0

First public release, under the AGPL-3.0.

### Added
- Print straight to a Brother PT-P950NW over the network, with a check that the right tape cassette is loaded before each job.
- **Print label** on a tag's page, and tick boxes with **Print selected** on the Tags list.
- Batches can come out cut into separate labels or as one strip with dashed cut lines, to cut by hand on site.
- Several label printers: each label goes to the first printer with the right tape loaded. Printers can be added, renamed, re-addressed, calibrated and removed on the Printer page.
- Per-printer vertical position calibration.
- Tape tracking: roughly how much tape is left in each cassette, from the printer's print-length counter, with a warning when it's low.
- Settings page and a first-run setup screen for your organisation's name, mark, accent colour, support contact, label wording, loan client, label formats, Hudu and sign-in options.
- Configurable label formats: switch formats on or off and choose the tape colour and description for each.
- Display names for users, editable on their account page; tag history shows names, not usernames.
- Sign in with your email address as well as your username.
- The device name on the public scan page.
- Version number and build in the footer, and at `/version`.
- Screenshots and drawn examples of tags on kit in the README, with QR codes you can scan ([#8](https://github.com/Fourth-Floor-Solutions/tagdesk/issues/8)).
- Set the Hudu API key, Microsoft sign-in (tenant, client ID, client secret and its expiry) and the source-code link in Settings, with **Test** buttons, changes applied without a restart, a warning before the client secret expires, and a log of setting changes. Values in `.env` still work and take priority ([#7](https://github.com/Fourth-Floor-Solutions/tagdesk/issues/7)).
- An application log (`docker compose logs`): sign-ins, tag events, settings changes, and printer, Hudu and Microsoft sign-in failures. Passwords and secrets are never logged. Set the level with `LOG_LEVEL` ([#10](https://github.com/Fourth-Floor-Solutions/tagdesk/issues/10)).
- This changelog, grouped by date, and a What's new page linked from the footer ([#13](https://github.com/Fourth-Floor-Solutions/tagdesk/issues/13)).

### Changed
- Renamed the project from Asset Tags to TagDesk.
- Tag status reads **Assigned** / **Not assigned** (to a device) instead of "On device" / "Not on device", which read oddly next to **Not printed**; the summary box "Printed, not yet on a device" is now **Not assigned yet**.
- When Microsoft sign-in is set up, the sign-in page shows only **Sign in with Microsoft**, with the local form behind **Use a local account**.
- Button text switches between black and white to suit the accent colour.
- The reverse proxy's address is a setting (`FORWARDED_ALLOW_IPS`), and so is the published port (`HOST_PORT`).
- The sign-in page has a background of drifting asset tags (a mix of 18, 12 and 6 mm sizes) in your accent colour, and no longer shows the version or a separate footer. Signed-in pages show a faint, still version of the same background ([#16](https://github.com/Fourth-Floor-Solutions/tagdesk/issues/16)).
- Batches print as one strip with cut lines by default; each browser remembers if you choose **Cut each label** instead ([#14](https://github.com/Fourth-Floor-Solutions/tagdesk/issues/14)). The choice is also on the **Print a batch** page and carries through to printing ([#15](https://github.com/Fourth-Floor-Solutions/tagdesk/issues/15)).
- Code quality: Ruff linting and formatting, each test runs against its own database, coverage raised to over 80% and checked in CI, local development uses Python 3.14 like the Docker image, and the app's routes are split into one module per area with a shared database helper and settings read once per request ([#10](https://github.com/Fourth-Floor-Solutions/tagdesk/issues/10)).
- Updated to Python 3.14, FastAPI 0.143, Starlette 1.8, uvicorn 0.54, Authlib 1.8, Pillow 12.3 and httpx 0.28.

### Fixed
- The saved accent colour was ignored and buttons showed the default blue ([#11](https://github.com/Fourth-Floor-Solutions/tagdesk/issues/11)).
- Labels printed mirrored and about 1 mm off-centre.
- The **Print label** button did nothing after an update, because browsers kept an old copy of the page's script. Script and stylesheet links now change with every release.
- Batches that were meant to print as one strip came out fully cut.
- The selection bar on the Tags list showed "0 selected" when nothing was ticked.
- Fields on the Settings page that share a row didn't line up ([#17](https://github.com/Fourth-Floor-Solutions/tagdesk/issues/17)).
- The Settings page's browser tab title included stray page content.

### Security
- Failed sign-ins are limited per IP address, and only the reverse proxy may pass on the visitor's IP.
- Secrets saved in Settings are encrypted, with the key kept outside the database, and are never shown again.
- Fixed redirects after sign-in so they can only go to a page on this site.
- Hudu links on tags must point at your configured Hudu server, so a link can't run a script.
- Microsoft sign-in refuses multi-tenant settings (`common`, `organizations`) and accounts from other organisations.
- Changing the Hudu address needs the API key entered again, so a saved key isn't sent to a new server.
- Unknown usernames and wrong passwords take the same time to check.
- Security headers (Content-Security-Policy, no framing, nosniff, referrer policy); cross-site form posts are refused; signing out is a form post.
- Public code lookups are limited per IP address.
- `SECURITY.md` explains how to report a vulnerability privately; tests and a dependency audit run on every pull request ([#9](https://github.com/Fourth-Floor-Solutions/tagdesk/issues/9)).

## 30 September 2026

First working version, used internally before release.

### Added
- Issue asset tags with permanent five-character codes and QR codes, in Standard, Slim, Micro and Loan formats.
- Print labels from the browser at their real size.
- Public scan page showing who owns a device and how to contact them.
- Batches of blank tags for tagging a site, filled in when each one is scanned.
- Hudu integration: client sync, linking tags to assets, and writing the tag code into the asset.
- Local accounts with admin and user roles, and optional Microsoft sign-in.
