import re

from fastapi.testclient import TestClient

from app.main import app


def login(c):
    from app import settings

    settings.mark_setup_done()
    r = c.post(
        "/login", data={"username": "admin", "password": "test-password-123", "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303


def internal_client_id(c):
    m = re.search(r'<option value="(\d+)" data-internal="1"', c.get("/tags/new").text)
    if not m:  # new installs create their own client during setup
        c.post(
            "/settings",
            data={"org_name": "Test Org", "org_mark": "TO", "accent": "#5b8def", "internal_client_id": "new"},
        )
        m = re.search(r'<option value="(\d+)" data-internal="1"', c.get("/tags/new").text)
    return m.group(1)


def test_flow():
    c = TestClient(app)
    assert c.get("/", follow_redirects=False).status_code == 303
    login(c)
    # add a client and issue a tag for it
    c.post("/clients/new", data={"name": "Example Ltd", "label_name": "Example"})
    page = c.get("/tags/new").text
    cid = re.search(r'<option value="(\d+)" data-internal="0"[^>]*>Example Ltd', page).group(1)
    r = c.post(
        "/tags/new",
        data={
            "client_id": cid,
            "format": "slim",
            "device_name": "EX01-SW02",
            "model": "USW-Pro-48",
            "serial": "AB12CD",
        },
        follow_redirects=False,
    )
    code = r.headers["location"].rsplit("/", 1)[1]
    t = c.get(f"/tags/{code}").text
    assert "Example" in t and "<svg" in t and "hasn't been printed" in t
    # duplicate serial refused
    r = c.post("/tags/new", data={"client_id": cid, "format": "standard", "serial": "AB12CD"})
    assert r.status_code == 400 and "already tagged" in r.text
    # loan format refused for a client
    r = c.post("/tags/new", data={"client_id": cid, "format": "loan"})
    assert r.status_code == 400
    # print + record, then rename client -> needs reprint
    assert "ready to print" in c.get(f"/print?codes={code}").text
    assert c.post("/print/record", data={"codes": code}).json() == {"recorded": 1}
    c.post(f"/clients/{cid}", data={"label_name": "Example UK", "active": "1"})
    assert "no longer matches" in c.get(f"/tags/{code}").text
    # scan: staff redirect; public sees owner only
    assert c.get(f"/{code.lower()}", follow_redirects=False).headers["location"] == f"/tags/{code}"
    pub = TestClient(app).get(f"/{code}")
    assert pub.status_code == 200 and "belongs to Example UK" in pub.text and "AB12CD" not in pub.text
    assert "EX01-SW02" in pub.text
    assert TestClient(app).get("/ZZZZZ").status_code == 404
    # batch
    r = c.post(
        "/tags/batch", data={"client_id": internal_client_id(c), "format": "loan", "count": "3"}, follow_redirects=False
    )
    assert r.headers["location"].startswith("/print?codes=")
    assert c.get(r.headers["location"]).text.count("<svg") == 3


def test_login_rate_limit():
    c = TestClient(app)
    for _ in range(10):
        assert c.post("/login", data={"username": "admin", "password": "wrong", "next": "/"}).status_code == 401
    assert c.post("/login", data={"username": "admin", "password": "wrong", "next": "/"}).status_code == 429
    from app import main

    main._login_fails.clear()


def test_hudu_sync(monkeypatch):
    from app import hudu

    calls = []
    monkeypatch.setattr(hudu, "write_enabled", lambda: True)
    monkeypatch.setattr(
        hudu,
        "set_tag",
        lambda aid, val, only_if=None: calls.append((aid, val, only_if)) or ("cleared" if val == "" else "written"),
    )
    c = TestClient(app)
    login(c)
    cid = internal_client_id(c)
    r = c.post(
        "/tags/new",
        data={
            "client_id": cid,
            "format": "standard",
            "device_name": "X",
            "hudu_asset_id": "111",
            "hudu_asset_url": "https://h/a/x",
        },
        follow_redirects=False,
    )
    code = r.headers["location"].rsplit("/", 1)[1]
    assert calls == [(111, code, None)]
    # relink to another asset: clear old (guarded), write new
    c.post(
        f"/tags/{code}",
        data={
            "client_id": cid,
            "format": "standard",
            "device_name": "X",
            "hudu_asset_id": "222",
            "hudu_asset_url": "https://h/a/y",
        },
    )
    assert calls[1:] == [(111, "", code), (222, code, None)]
    # unrelated edit: no Hudu calls
    c.post(
        f"/tags/{code}",
        data={
            "client_id": cid,
            "format": "standard",
            "device_name": "X2",
            "hudu_asset_id": "222",
            "hudu_asset_url": "https://h/a/y",
        },
    )
    assert len(calls) == 3
    # retire clears it
    c.post(f"/tags/{code}/status", data={"status": "retired"})
    assert calls[3:] == [(222, "", code)]
    assert "cleared" in c.get(f"/tags/{code}").text


def test_login_by_email(conn):
    from app import main

    conn.execute("UPDATE users SET email = 'admin@example.test' WHERE username = 'admin'")
    conn.close()
    main._login_fails.clear()
    c = TestClient(app)
    r = c.post(
        "/login",
        data={"username": "Admin@Example.test", "password": "test-password-123", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_printer_encoding():
    from app import labels, printer

    img = labels.raster("slim", "7KQ3M", "Example", "https://asset.example")
    assert img.size == (539, 170)
    lines = printer.raster_lines(img, 12)
    assert len(lines) == 539 - 2 * printer.FEED_DOTS and all(len(line) == 70 for line in lines)
    # nothing outside the 12 mm print area pins (213..362: after the 213-pin right margin)
    for line in lines:
        bits = int.from_bytes(line, "big")
        for pin in list(range(0, 213)) + list(range(363, 560)):
            assert not bits >> (559 - pin) & 1
    # columns go right to left: ink at the label's left edge is the last line sent
    from PIL import Image

    probe = Image.new("1", img.size, 1)
    probe.putpixel((printer.FEED_DOTS, 80), 0)
    probe_lines = printer.raster_lines(probe, 12)
    assert any(probe_lines[-1]) and not any(any(line) for line in probe_lines[:-1])
    assert b"\x1biK\x08" in printer.build_job([img], 12)  # never chain printing
    job = printer.build_job([img], 12)
    assert job.startswith(b"\x00" * 200 + b"\x1b@\x1bia\x01\x1biz\x84\x00\x0c\x00") and job.endswith(b"\x1a")


def test_check_tape():
    from app import printer

    st = {"errors": [], "tape_mm": 12, "tape_colour": "yellow"}
    assert printer.check_tape(st, "slim") is None
    assert "18 mm" in printer.check_tape(st, "standard")
    assert "red" in printer.check_tape(dict(st, tape_mm=18), "loan")


def test_nudge_moves_print():
    from PIL import Image

    from app import printer

    probe = Image.new("1", (100, 170), 1)
    probe.putpixel((50, 80), 0)

    def pin(nudge):
        line = next(row for row in printer.raster_lines(probe, 12, nudge) if any(row))
        v = int.from_bytes(line, "big")
        return next(p for p in range(560) if v >> (559 - p) & 1)

    assert pin(-14) == pin(0) - 14


def test_print_buttons(monkeypatch):
    from app import main, printer

    main._login_fails.clear()
    c = TestClient(app)
    login(c)
    monkeypatch.setattr(printer, "status", lambda host: (_ for _ in ()).throw(printer.PrinterError("offline")))
    c.post("/printers/new", data={"name": "Office", "address": "127.0.0.1"})
    c.post("/tags/new", data={"client_id": internal_client_id(c), "format": "standard", "device_name": "D"})
    page = c.get("/").text
    assert 'id="print-selected"' in page and 'name="pick"' in page
    code = __import__("re").search(r'href="/tags/([0-9A-Z]{5})"', page).group(1)
    assert f'data-print-direct="{code}"' in c.get(f"/tags/{code}").text


def test_printers_and_routing(monkeypatch):
    from app import main, printer

    assert printer.clean_host("http://192.168.1.50/") == "192.168.1.50"
    assert printer.clean_host("printer.office.lan") == "printer.office.lan"
    assert printer.clean_host("bad host; rm -rf") is None
    main._login_fails.clear()
    c = TestClient(app)
    login(c)
    tapes = {"10.0.0.1": (12, "yellow"), "10.0.0.2": (18, "yellow")}

    def fake_status(host):
        if host not in tapes:
            raise printer.PrinterError("offline")
        return {
            "model": "PT-P950NW",
            "errors": [],
            "tape_mm": tapes[host][0],
            "tape_colour": tapes[host][1],
            "laminated": True,
            "busy": False,
        }

    monkeypatch.setattr(printer, "status", fake_status)
    sent = []
    monkeypatch.setattr(printer, "send", lambda host, job: sent.append(host))
    monkeypatch.setattr(printer, "wait_done", lambda host: None)
    c.post("/printers/new", data={"name": "Twelve", "address": "http://10.0.0.1/"})
    c.post("/printers/new", data={"name": "Eighteen", "address": "10.0.0.2"})
    c.post("/printers/new", data={"name": "Bad", "address": "not valid!"})
    page = c.get("/printer").text
    assert "Twelve" in page and "Eighteen" in page and "10.0.0.1" in page and "Bad" not in page
    # one slim + one standard tag: each routed to the printer with matching tape
    cid = internal_client_id(c)
    codes = []
    for fmt in ("slim", "standard"):
        r = c.post("/tags/new", data={"client_id": cid, "format": fmt, "device_name": fmt}, follow_redirects=False)
        codes.append(r.headers["location"].rsplit("/", 1)[1])
    d = c.post("/print/send", data={"codes": ",".join(codes)}).json()
    assert d["ok"] and sorted(d["printed"]) == sorted(codes) and sorted(sent) == ["10.0.0.1", "10.0.0.2"]
    assert "Twelve" in d["message"] and "Eighteen" in d["message"]


def test_static_urls_are_versioned():
    c = TestClient(app)
    html = c.get("/login").text
    import re

    m = re.search(r'src="/static/app\.js\?v=([0-9a-f]{10})"', html)
    assert m and re.search(r'href="/static/app\.css\?v=[0-9a-f]{10}"', html)
    assert c.get(f"/static/app.js?v={m.group(1)}").status_code == 200


def test_display_name():
    from app import main

    main._login_fails.clear()
    c = TestClient(app)
    login(c)
    c.post("/account/name", data={"name": "Sam Taylor"})
    assert '<a href="/account">Sam Taylor</a>' in c.get("/").text
    cid = internal_client_id(c)
    r = c.post("/tags/new", data={"client_id": cid, "format": "standard", "device_name": "N"}, follow_redirects=False)
    code = r.headers["location"].rsplit("/", 1)[1]
    assert "by Sam Taylor" in c.get(f"/tags/{code}").text


def test_tel_link():
    from app.main import tel_link

    assert tel_link("+44 (0)20 7946 0000") == "+442079460000"
    assert tel_link("020 7946 0000") == "02079460000"


def test_strip_job():
    from app import labels, printer

    imgs = [labels.raster("slim", c, "X", "https://a.example") for c in ("AAAAA", "BBBBB", "CCCCC")]
    strips = printer.join_strip(imgs)
    assert len(strips) == 1 and strips[0].width == sum(i.width for i in imgs)
    # dashed cut line at each join
    for x in (imgs[0].width, imgs[0].width * 2):
        assert any(strips[0].getpixel((x, y)) == 0 for y in range(strips[0].height))
    job = printer.build_job(imgs, 12, strip=True)
    assert job.count(b"\x1bia\x01") == 1 and job.count(b"\x1biK\x08") == 1  # one page, one cut
    single = printer.build_job(imgs, 12)
    assert single.count(b"\x1bia\x01") == 3  # three pages, three cuts
    # very long batches split into several strips under 1 m
    many = printer.join_strip([labels.raster("standard", "AAAAA", "X", "https://a.example")] * 30)
    assert len(many) == 2 and all(s.width <= printer.MAX_PAGE_DOTS for s in many)


def test_cassette_usage(monkeypatch, conn):
    from app import db, printer, usage

    raw = bytes.fromhex(
        "D2010400 00003C D1010400 000012 00010400 000033 60010400 0000FB 12000400 000000 FF".replace(" ", "")
    )
    monkeypatch.setattr(printer, "_snmp_get", lambda host, oid: raw)
    assert printer.counter_cm("x") == 251
    conn.execute("INSERT INTO printers(name, host, created_at) VALUES('U','10.9.9.9',?)", (db.now(),))
    p = conn.execute("SELECT * FROM printers WHERE host='10.9.9.9'").fetchone()
    st = {"tape_mm": 18, "tape_colour": "yellow", "errors": []}
    counts = iter([1000, 1030, 1030, 1100])
    monkeypatch.setattr(printer, "counter_cm", lambda host: next(counts))
    usage.track(conn, p, st)  # baseline
    usage.track(conn, p, st)  # +30 cm on 18 yellow
    st12 = {"tape_mm": 12, "tape_colour": "yellow", "errors": []}
    usage.track(conn, p, st12)  # swapped, no printing
    usage.track(conn, p, st12)  # +70 cm on 12 yellow
    s18 = usage.summary(conn, p["id"], st)
    s12 = usage.summary(conn, p["id"], st12)
    assert s18["used_cm"] == 30 and s12["used_cm"] == 70 and s18["left_cm"] == 770
    assert s18["labels_batch"] == int(770 // 5.0)


def test_setup_and_settings(conn):
    from app import main, settings

    conn.execute("DELETE FROM settings WHERE key = 'setup_done'")
    conn.close()
    main._login_fails.clear()
    c = TestClient(app)
    c.post("/login", data={"username": "admin", "password": "test-password-123", "next": "/"})
    assert c.get("/", follow_redirects=False).headers["location"] == "/setup"  # first admin -> setup
    r = c.post(
        "/settings",
        data={
            "setup": "1",
            "org_name": "Acme IT Ltd",
            "org_short": "Acme",
            "org_mark": "AC",
            "accent": "#00aaff",
            "support_phone": "+44 (0)1234 567890",
            "label_managed": "Managed by {name}",
            "label_loan_top": "{SHORT} LOAN",
            "label_loan_bottom": "Return to {short}",
            "internal_client_id": "",
        },
        follow_redirects=False,
    )
    assert r.headers["location"] == "/printer" and settings.setup_done()
    org = settings.get_all()
    t = settings.label_text(org)
    assert (t["managed"], t["loan_top"], t["loan_bottom"], t["mark"]) == (
        "Managed by Acme IT Ltd",
        "ACME LOAN",
        "Return to Acme",
        "AC",
    )
    page = c.get("/").text
    assert "--accent: #00aaff" in page and ">AC</span>" in page
    assert "AC" in c.get("/favicon.svg").text
    # bad colour refused
    assert c.post("/settings", data={"org_name": "Acme", "org_mark": "AC", "accent": "blue"}).status_code == 400
    settings.save(dict(settings.DEFAULTS))


def test_login_page_sso_only(monkeypatch):
    from app import auth

    monkeypatch.setattr(auth, "sso_enabled", lambda: True)
    html = TestClient(app).get("/login").text
    assert "Sign in with Microsoft" in html and '<details class="local-login"' in html
    monkeypatch.setattr(auth, "sso_enabled", lambda: False)
    assert '<details class="local-login"' not in TestClient(app).get("/login").text


def test_label_formats_settings():
    from app import labels, main, printer, settings

    main._login_fails.clear()
    c = TestClient(app)
    login(c)
    data = {
        "org_name": "Test Org",
        "org_mark": "TO",
        "accent": "#5b8def",
        "internal_client_id": internal_client_id(c),
        "formats_shown": "1",
    }
    for k, f in labels.BUILT_IN.items():
        data.update({f"fmt_{k}_enabled": "1", f"fmt_{k}_colour": f["tape_colour"], f"fmt_{k}_tape": f["tape"]})
    data.update({"fmt_slim_colour": "white", "fmt_slim_tape": "TZe-231 · 12 mm black on white"})
    data.pop("fmt_loan_enabled")
    c.post("/settings", data=data)
    assert labels.FORMATS["slim"]["tape_colour"] == "white" and not labels.FORMATS["loan"]["enabled"]
    assert settings.formats()["slim"]["tape"] == "TZe-231 · 12 mm black on white"
    assert 'value="loan"' not in c.get("/tags/new").text  # switched-off format hidden
    st = {"errors": [], "tape_mm": 12, "tape_colour": "white"}
    assert printer.check_tape(st, "slim") is None and "white" in printer.check_tape(
        dict(st, tape_colour="yellow"), "slim"
    )
    assert labels.fill("slim") == labels.TAPE_FILLS["white"]
    r = c.post("/tags/new", data={"client_id": data["internal_client_id"], "format": "loan"})
    assert r.status_code == 400 and "switched off" in r.text
    # at least one client format must stay on
    off = {k: v for k, v in data.items() if not k.endswith("_enabled")}
    assert c.post("/settings", data=off).status_code == 400
    settings.save_formats({})
    labels.apply_overrides({})


def test_accent_ink():
    from app.settings import ink_for

    assert ink_for("#f6f06b") == "#16181c"  # dark text on yellow
    assert ink_for("#1f3a8a") == "#ffffff"  # white text on navy
    assert ink_for("nonsense") == "#16181c"


def test_legacy_database_name_is_renamed(tmp_path, monkeypatch):
    import sqlite3

    from app import db

    tmp_path = tmp_path / "legacy"  # its own folder: the fixture already made tagdesk.db in tmp_path
    tmp_path.mkdir()
    old = tmp_path / "asset-tags.db"
    setup = sqlite3.connect(old)
    setup.execute("CREATE TABLE t(x)")
    setup.commit()
    setup.close()
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "tagdesk.db"))
    monkeypatch.setattr(db, "LEGACY_DB_PATH", str(old))
    conn = db.connect()
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 't'").fetchone()
    assert (tmp_path / "tagdesk.db").exists() and not old.exists()
    conn.close()


def test_saved_accent_overrides_stylesheet_default():
    """The inline accent must come after app.css, or the stylesheet's default wins."""
    from app import settings

    settings.save({"accent": "#00aaff"})
    try:
        html = TestClient(app).get("/login").text
        assert html.index("--accent: #00aaff") > html.index("/static/app.css")
    finally:
        settings.save({"accent": settings.DEFAULTS["accent"]})


def test_version_in_footer_and_endpoint(monkeypatch):
    from app import __version__, version

    c = TestClient(app)
    assert c.get("/version").json()["version"] == __version__
    html = c.get("/changelog").text
    assert f"v{__version__}" in html and f"/releases/tag/v{__version__}" in html
    monkeypatch.setattr(version, "COMMIT", "abc1234def")
    assert "abc1234" in TestClient(app).get("/changelog").text
    monkeypatch.setenv("GIT_COMMIT", "not a commit!")
    assert version._commit() == ""


def test_batch_cut_choice_carries_to_print_page(monkeypatch):
    from app import main, printer

    main._login_fails.clear()
    c = TestClient(app)
    login(c)
    monkeypatch.setattr(printer, "status", lambda host: (_ for _ in ()).throw(printer.PrinterError("offline")))
    c.post("/printers/new", data={"name": "Office", "address": "127.0.0.1"})  # the cut choice needs a printer
    assert 'name="cut"' in c.get("/tags/batch").text
    cid = internal_client_id(c)
    r = c.post(
        "/tags/batch", data={"client_id": cid, "format": "standard", "count": "2", "cut": "cut"}, follow_redirects=False
    )
    assert r.headers["location"].endswith("&cut=cut")
    assert 'data-initial="cut"' in c.get(r.headers["location"]).text
    r = c.post("/tags/batch", data={"client_id": cid, "format": "standard", "count": "2"}, follow_redirects=False)
    assert r.headers["location"].endswith("&cut=strip")


def test_login_page_tag_streams_and_no_version():
    from app import __version__

    html = TestClient(app).get("/login").text
    assert 'class="tag-streams" aria-hidden="true"' in html and html.count('class="mtag ') >= 100
    assert all(f'class="mtag {s}"' in html for s in ("t18", "t12", "t6"))  # a mix of tape sizes
    assert f"v{__version__}" not in html and 'class="foot' not in html
    card = html[html.index('class="card narrow login-card"') :]
    assert 'class="login-src"' in card and "source code" in card  # AGPL link inside the card


def test_static_tag_background_only_when_signed_in():
    from app import main

    main._login_fails.clear()
    anon = TestClient(app).get("/ZZZZZ").text  # public scan page, signed out
    assert "tag-streams" not in anon
    c = TestClient(app)
    login(c)
    html = c.get("/").text
    assert 'class="tag-streams static"' in html and "--speed" not in html


def test_settings_page_title_is_plain():
    import re as _re

    from app import main

    main._login_fails.clear()
    c = TestClient(app)
    login(c)
    title = _re.search(r"<title>(.*?)</title>", c.get("/settings").text, _re.S).group(1)
    assert "<" not in title and title.startswith("Settings")


def test_status_wording_is_about_assignment():
    from app import main

    main._login_fails.clear()
    c = TestClient(app)
    login(c)
    cid = internal_client_id(c)
    c.post("/tags/batch", data={"client_id": cid, "format": "standard", "count": "1"})
    page = c.get("/").text
    assert "Not assigned" in page and "Assigned to a device" in page
    assert "On device" not in page and "Printed, not yet on a device" not in page
