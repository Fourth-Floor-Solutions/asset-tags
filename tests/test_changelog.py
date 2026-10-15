import pytest

from app import changelog


def test_repo_changelog_is_valid():
    """Fails if CHANGELOG.md repeats a date, has dates out of order or uses an unknown section."""
    days = changelog.load()
    assert days, "CHANGELOG.md has no dated sections"
    from app import __version__

    assert changelog.for_version(days, __version__), f"v{__version__} isn't under any date in CHANGELOG.md"


def _days(text):
    days = changelog.parse(text)
    changelog.check(days)
    return days


def test_rejects_repeated_date():
    with pytest.raises(changelog.ChangelogError, match="more than once"):
        _days("## 2 May 2026\n### Added\n- a\n## 2 May 2026\n### Fixed\n- b\n")


def test_rejects_out_of_order():
    with pytest.raises(changelog.ChangelogError, match="out of order"):
        _days("## 1 May 2026\n### Added\n- a\n## 2 May 2026\n### Added\n- b\n")


def test_rejects_unknown_section_and_bad_heading():
    with pytest.raises(changelog.ChangelogError, match="sections are"):
        _days("## 2 May 2026\n### Misc\n- a\n")
    with pytest.raises(changelog.ChangelogError, match="date headings"):
        _days("## v1.0.0\n- a\n")


def test_versions_and_inline_html():
    d = _days(
        "## 2 May 2026 · v1.2.0, v1.2.1\n### Fixed\n- A **bold** fix, see [#3](https://example.com/3) and `x<y`\n"
    )[0]
    assert d["versions"] == ["v1.2.0", "v1.2.1"]
    html = changelog.inline_html(d["sections"]["Fixed"][0])
    assert "<b>bold</b>" in html and '<a href="https://example.com/3"' in html and "<code>x&lt;y</code>" in html
    assert "<script>" not in changelog.inline_html("<script>alert(1)</script>")
    assert "javascript:" not in changelog.inline_html("[x](javascript:alert(1))").split('href="')[-1][:12]


def test_whats_new_page():
    from fastapi.testclient import TestClient

    from app.main import app

    html = TestClient(app).get("/changelog").text
    assert "What's new" in html and "15 October 2026" in html and "Print label" in html
