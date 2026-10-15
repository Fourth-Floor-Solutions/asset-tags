# Changelog

All notable changes to TagDesk. Versions follow [semantic versioning](https://semver.org).

## [0.1.0] - 2026-10-15

First public release.

- Issue client-branded asset tags with permanent five-character codes and QR codes.
- Print straight to a Brother PT-P950NW (raster over TCP 9100, status over SNMP), with a check that the right cassette is loaded, per-printer calibration, several printers, tape-use tracking, and batches cut separately or as one strip with cut lines.
- Public scan page showing the owner, device name and support contact.
- Optional Hudu integration: client sync, asset linking, and writing the tag code into the asset.
- Local accounts and optional Microsoft Entra ID sign-in, with admin and user roles.
- Settings page and first-run setup for organisation details, label wording and formats, Hudu and sign-in options.
- Version number and build commit in the footer and at `/version`.
