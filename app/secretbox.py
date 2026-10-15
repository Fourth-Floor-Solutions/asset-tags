"""Encrypting secrets (API keys, client secrets) stored in the database.

The key is never in the database: it comes from TAGDESK_SECRET_KEY in the
environment, or from `secret.key` in the data folder, created on first use
with owner-only permissions. A copy of the database on its own therefore
doesn't reveal the secrets. If the key changes or is lost, decrypt() raises
SecretUnreadable so the app can ask for the secret to be entered again.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from . import db

KEY_FILE = "secret.key"
PREFIX = "enc:v1:"


class SecretUnreadable(Exception):
    pass


def _key_path() -> str:
    return os.path.join(db.DATA_DIR, KEY_FILE)


def _key() -> bytes:
    env = os.environ.get("TAGDESK_SECRET_KEY", "").strip()
    if env:
        return env.encode()
    path = _key_path()
    if not os.path.exists(path):
        os.makedirs(db.DATA_DIR, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    with open(path, "rb") as f:
        return f.read().strip()


def encrypt(value: str) -> str:
    return PREFIX + Fernet(_key()).encrypt(value.encode()).decode()


def decrypt(stored: str | None) -> str | None:
    if not stored:
        return None
    if not stored.startswith(PREFIX):
        raise SecretUnreadable("The stored value isn't in the expected format.")
    try:
        return Fernet(_key()).decrypt(stored[len(PREFIX) :].encode()).decode()
    except (InvalidToken, ValueError) as e:
        raise SecretUnreadable(
            "It was saved with a different encryption key (the data folder's secret.key or "
            "TAGDESK_SECRET_KEY changed). Enter it again."
        ) from e
