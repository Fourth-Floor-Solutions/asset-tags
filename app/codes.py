"""Tag codes: five characters from the Crockford Base32 alphabet.

The alphabet leaves out I, L, O and U so a code can't be misread. When a code
is typed or scanned, O is read as 0 and I/L as 1, so common slips still resolve.
"""
from __future__ import annotations

import re
from typing import Optional

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
LENGTH = 5
SPACE = len(ALPHABET) ** LENGTH  # 33,554,432 possible codes

_STRIP = re.compile(r"[\s\-_.]")


def normalise(raw: str) -> str:
    """Upper-case, drop separators and map look-alikes (O->0, I/L->1)."""
    s = _STRIP.sub("", raw or "").upper()
    return s.replace("O", "0").replace("I", "1").replace("L", "1")


def is_valid(code: str) -> bool:
    return len(code) == LENGTH and all(ch in ALPHABET for ch in code)


def parse(raw: str) -> Optional[str]:
    """Return the canonical code for user input, or None if it can't be one."""
    code = normalise(raw)
    return code if is_valid(code) else None


def to_num(code: str) -> int:
    n = 0
    for ch in code:
        n = n * 32 + ALPHABET.index(ch)
    return n


def to_code(n: int) -> str:
    if not 0 <= n < SPACE:
        raise ValueError("code number out of range")
    out = []
    for _ in range(LENGTH):
        out.append(ALPHABET[n % 32])
        n //= 32
    return "".join(reversed(out))
