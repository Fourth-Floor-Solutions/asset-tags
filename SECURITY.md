# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Report it
privately through GitHub: **Security → Report a vulnerability** on this
repository (private vulnerability reporting is enabled). Include what you
found, how to reproduce it, and the version (shown in the app's footer and at
`/version`).

We'll acknowledge reports within a few working days, keep you updated, and
credit you in the release notes if you'd like.

## Supported versions

Fixes go into the latest release. Please update to it before reporting.

## What's in scope

The TagDesk application in this repository: sign-in and sessions, the web
pages and their forms, the public scan page, the Hudu and Microsoft
integrations, label printing, and the Docker image and compose file.

Out of scope: the security of your reverse proxy, network, Hudu instance,
Microsoft tenant or label printer, and findings that need an admin account to
misuse admin-only features (admins are trusted to configure integrations and
printers).
