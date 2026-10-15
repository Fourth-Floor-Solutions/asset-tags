"""The running version, and the git commit it was built from when known.

The commit comes from the GIT_COMMIT environment variable (a Docker build
argument), or from app/BUILD written at deploy time. Neither is required.
"""
from __future__ import annotations

import os
import re

from . import __version__

VERSION = __version__


def _commit() -> str:
    value = os.environ.get("GIT_COMMIT", "").strip()
    if not value:
        try:
            with open(os.path.join(os.path.dirname(__file__), "BUILD")) as f:
                value = f.read().strip()
        except OSError:
            value = ""
    value = value[:12]
    return value if re.fullmatch(r"[0-9a-f]{7,12}", value) else ""


COMMIT = _commit()


def label() -> str:
    return f"v{VERSION}" + (f" ({COMMIT[:7]})" if COMMIT else "")
