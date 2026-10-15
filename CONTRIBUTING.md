# Contributing to TagDesk

Thanks for helping. Bug reports, ideas and pull requests are all welcome.

- **Bugs and ideas:** open an issue. For a bug, say what you did, what you
  expected and what happened, and include your TagDesk version (shown in
  the footer and at `/version`).
- **Security problems:** please don't open a public issue. Report them
  privately as described in [SECURITY.md](SECURITY.md).
- **Bigger changes:** open an issue first so we can agree on the approach
  before you spend time on it.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains how the pieces fit
together: labels, printing, tape tracking, settings, Hudu and sign-in.

## Set up

TagDesk targets Python 3.14, the same as the Docker image. With
[uv](https://docs.astral.sh/uv/):

```sh
uv python install 3.14
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt
cp .env.example .env    # set ADMIN_PASSWORD, PUBLIC_BASE_URL=http://localhost:8080, COOKIE_INSECURE=1
./run-dev.sh            # http://localhost:8080, reloads when you save
```

Sign in as `admin` with the `ADMIN_PASSWORD` from `.env`. You don't need a
label printer, Hudu or Microsoft sign-in to work on most of the app.
Printing to a browser works without a printer, and Hudu and sign-in stay
off until they're set up in Settings.

## Check your change

The same checks run in CI on every pull request:

```sh
.venv/bin/ruff check . && .venv/bin/ruff format --check .   # lint and format
.venv/bin/mypy                                              # type check
.venv/bin/pytest --cov=app                                  # tests; CI fails below 80% coverage
```

`ruff format .` fixes formatting. Tests run against a fresh database each
time and never talk to a real printer or Hudu. Use the fakes in
`tests/test_hudu.py` and the SNMP stubs in `tests/conftest.py`. An unclosed
database connection fails the run: use `with db.connection() as conn:`.

If you change the page layout, check it on a phone-sized window too (720px
and narrower uses the folded **Menu**).

## Pull requests

- Keep each pull request to one change, with a short description of what
  and why. Link the issue it fixes.
- Add or update tests for behaviour you change.
- **Changelog:** add a line for every change users will notice under
  **today's date** in [CHANGELOG.md](CHANGELOG.md), in the same pull
  request. If today's date is already there, add to it; never start a
  second heading for the same day. Use `### Added`, `### Changed`,
  `### Fixed`, `### Security` or `### Removed`. The tests fail on a
  repeated or out-of-order date.
- Write for the people using the app: plain words, no internal jargon.
- `main` is protected: changes go through a pull request, the lint, test
  and image checks must pass, and pull requests are squash-merged.

## Dependencies

`requirements.in` lists the app's direct dependencies with version ranges.
`requirements.txt` is generated from it with every package pinned to an
exact version and hash, and it's what the Docker image installs. After
changing `requirements.in`, regenerate the lock file:

```sh
uv pip compile requirements.in --universal --generate-hashes --python-version 3.14 -o requirements.txt
```

To update everything within the ranges, add `--upgrade`. Development tools
are in `requirements-dev.txt`. Dependabot proposes updates weekly, and CI
audits the installed packages for known vulnerabilities.

## Releases

Versions follow [semantic versioning](https://semver.org). To release:

1. Bump `__version__` in `app/__init__.py`.
2. Add the version to today's heading in `CHANGELOG.md`, e.g.
   `## 20 October 2026 · v0.2.0`.
3. After merging, tag the commit (`v0.2.0`) and create a GitHub release.
   Get its notes from `python -m scripts.release_notes v0.2.0`.

## Licence

TagDesk is licensed under the [AGPL-3.0](LICENSE). By contributing, you
agree that your contribution is licensed under the same terms.
