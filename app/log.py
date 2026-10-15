"""Application log: sign-ins, tag events (issued, printed, retired, Hudu writes), settings
changes and failures talking to printers, Hudu or Microsoft.

Lines go to stderr under the "tagdesk" logger, so `docker compose logs` shows them next to
uvicorn's own. LOG_LEVEL sets the level (default INFO). Passwords, secrets and tokens are
never logged.
"""

from __future__ import annotations

import logging
import os

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def get(area: str) -> logging.Logger:
    return logging.getLogger(f"tagdesk.{area}")


def setup() -> None:
    root = logging.getLogger("tagdesk")
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(handler)
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
