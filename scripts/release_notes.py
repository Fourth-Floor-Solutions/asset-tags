"""Print the release notes for a version, taken from its date in CHANGELOG.md.

    python -m scripts.release_notes v0.1.0 > notes.md
    gh release create v0.1.0 --notes-file notes.md
"""
import sys

from app import changelog

days = changelog.load()
day = changelog.for_version(days, sys.argv[1])
if not day:
    sys.exit(f"{sys.argv[1]} isn't in CHANGELOG.md")
print(f"Released {changelog.label(day['date'])}.\n")
print(changelog.to_markdown(day))
