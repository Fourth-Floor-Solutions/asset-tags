"""CHANGELOG.md, read and checked.

The changelog is grouped by date: one "## <day> <Month> <year>" heading per
day, newest first, optionally followed by " · v1.2.3" for versions released
that day. Under each date, "### Added / Changed / Fixed / Security / Removed"
sections hold "- " bullet points. Everything on the same day goes under one
heading; `check()` rejects repeated or out-of-order dates.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime
from html import escape
from typing import Optional

PATH = os.path.join(os.path.dirname(__file__), "..", "CHANGELOG.md")
TYPES = ("Added", "Changed", "Fixed", "Security", "Removed")

_DATE = re.compile(r"^## (\d{1,2} [A-Z][a-z]+ \d{4})(?:\s+·\s+(.+))?\s*$")
_VERSION = re.compile(r"^v\d+\.\d+\.\d+$")


class ChangelogError(ValueError):
    pass


def parse(text: str) -> list:
    """[{date, versions, intro, sections: {type: [items]}}], in file order."""
    days, current, section = [], None, None
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            m = _DATE.match(line)
            if not m:
                raise ChangelogError(f"Line {n}: date headings look like '## 15 October 2026' (got {line!r}).")
            try:
                day = datetime.strptime(m.group(1), "%d %B %Y").date()
            except ValueError as e:
                raise ChangelogError(f"Line {n}: {m.group(1)!r} isn't a valid date.") from e
            versions = [v.strip() for v in (m.group(2) or "").split(",") if v.strip()]
            for v in versions:
                if not _VERSION.match(v):
                    raise ChangelogError(f"Line {n}: versions look like v1.2.3 (got {v!r}).")
            current = {"date": day, "versions": versions, "intro": [], "sections": {}}
            days.append(current)
            section = None
        elif line.startswith("### ") and current is not None:
            name = line[4:].strip()
            if name not in TYPES:
                raise ChangelogError(f"Line {n}: sections are {', '.join(TYPES)} (got {name!r}).")
            if name in current["sections"]:
                raise ChangelogError(f"Line {n}: {name} appears twice under {current['date']:%d %B %Y}.")
            section = current["sections"].setdefault(name, [])
        elif line.startswith("- ") and section is not None:
            section.append(line[2:].strip())
        elif line.startswith("  ") and section:
            section[-1] += " " + line.strip()           # wrapped bullet
        elif line.strip() and current is not None and section is None:
            current["intro"].append(line.strip())
    return days


def check(days: list) -> None:
    """Raise ChangelogError if a date repeats or dates aren't newest first."""
    seen = set()
    for i, d in enumerate(days):
        if d["date"] in seen:
            raise ChangelogError(f"{d['date']:%d %B %Y} appears more than once; put everything from that day under one heading.")
        seen.add(d["date"])
        if i and d["date"] > days[i - 1]["date"]:
            raise ChangelogError(f"{d['date']:%d %B %Y} is out of order; newest dates go first.")
        if not d["sections"] and not d["intro"]:
            raise ChangelogError(f"{d['date']:%d %B %Y} has no entries.")


def load(path: str = PATH) -> list:
    with open(path, encoding="utf-8") as f:
        days = parse(f.read())
    check(days)
    return days


def for_version(days: list, version: str) -> Optional[dict]:
    """The date section a version was released in (for release notes)."""
    v = version if version.startswith("v") else f"v{version}"
    return next((d for d in days if v in d["versions"]), None)


def to_markdown(day: dict) -> str:
    out = list(day["intro"])
    for name in TYPES:
        items = day["sections"].get(name)
        if items:
            out += ["", f"### {name}"] + [f"- {i}" for i in items]
    return "\n".join(out).strip() + "\n"


_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def inline_html(text: str) -> str:
    """Escape, then allow the little Markdown the changelog uses: links (http/https only), `code`, **bold**."""
    html = escape(text, quote=True)
    html = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", html)
    html = _BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", html)
    html = _LINK.sub(lambda m: f'<a href="{m.group(2)}" rel="noopener">{m.group(1)}</a>', html)
    return html


def label(d: date) -> str:
    return f"{d.day} {d:%B %Y}"
