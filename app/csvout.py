"""CSV downloads that open cleanly in Excel and can't run spreadsheet formulas."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from fastapi.responses import Response


def cell(v) -> str:
    """A value a spreadsheet won't treat as a formula (CSV injection): =, +, -, @ get a leading '."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def safe_name(name: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in name).strip("-")[:40] or "export"


def response(header: list[str], rows: Iterable[Iterable], filename: str) -> Response:
    """UTF-8 with a byte-order mark, so Excel reads accented names correctly."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for row in rows:
        w.writerow([cell(v) for v in row])
    return Response(
        "﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
