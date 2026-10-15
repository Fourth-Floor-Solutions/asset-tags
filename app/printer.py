"""Direct printing to a Brother PT-P900-series printer (PT-P950NW) over the network.

- Status: the printer's 32-byte status block, read over SNMP (port 9100 is
  send-only on this model's network interface).
- Print: Brother raster commands sent raw to TCP 9100, per the
  "Raster Command Reference PT-P900/P900W/P950NW/P910BT" v1.02.
"""

from __future__ import annotations

import socket
import threading
import time

from . import labels

PORT = 9100
HEAD_PINS = 560
LINE_BYTES = HEAD_PINS // 8  # 70 bytes per raster line
FEED_DOTS = 14  # 1 mm leading/trailing margin, the minimum

# TZe tape width (mm) -> (pins for left margin, print area pins, width offset in dots).
# From the raster reference, sections 2.3.2 and 2.3.5.
TAPES = {
    4: (248, 48, 0),
    6: (240, 64, 10),
    9: (219, 106, 11),
    12: (197, 150, 10),
    18: (155, 234, 11),
    24: (112, 320, 10),
    36: (45, 454, 29),
}
TAPE_COLOURS = {
    0x01: "white",
    0x03: "clear",
    0x04: "red",
    0x05: "blue",
    0x06: "yellow",
    0x07: "green",
    0x08: "black",
    0x31: "red",
    0x41: "yellow",
}
ERRORS_1 = {
    0x01: "No tape loaded",
    0x02: "End of tape",
    0x04: "Cutter jam",
    0x08: "Weak batteries",
    0x40: "High-voltage adapter",
}
ERRORS_2 = {
    0x01: "Wrong tape for this job",
    0x02: "Expansion buffer full",
    0x04: "Communication error",
    0x08: "Communication buffer full",
    0x10: "Cover open",
    0x20: "Overheating",
    0x80: "System error",
}

_locks: dict = {}
_locks_guard = threading.Lock()


def _lock_for(host: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(host, threading.Lock())


class PrinterError(Exception):
    pass


_HOST_RE = __import__("re").compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")


def clean_host(raw: str) -> str | None:
    """Normalise what an admin typed ("http://192.168.1.50/" -> "192.168.1.50").

    Returns "" for blank, None if it isn't a plain IP address or host name.
    """
    s = (raw or "").strip()
    for prefix in ("http://", "https://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix) :]
    s = s.split("/")[0].strip()
    if not s:
        return ""
    return s if _HOST_RE.match(s) else None


# ---------- status over SNMP ----------

STATUS_OID = "1.3.6.1.4.1.2435.3.3.9.1.6.1.0"


def _ber_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _ber_len(len(value)) + value


def _oid(dotted: str) -> bytes:
    parts = [int(p) for p in dotted.split(".")]
    out = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(reversed(chunk))
    return _tlv(0x06, out)


def _read_tlv(buf: bytes, i: int):
    tag = buf[i]
    n = buf[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(buf[i : i + k], "big")
        i += k
    return tag, buf[i : i + n], i + n


def _snmp_get(host: str, oid: str, community: str = "public", timeout: float = 3.0) -> bytes:
    req_id = int(time.time() * 1000) & 0x7FFFFFFF
    varbind = _tlv(0x30, _oid(oid) + b"\x05\x00")
    pdu = _tlv(0xA0, _tlv(0x02, req_id.to_bytes(4, "big")) + b"\x02\x01\x00\x02\x01\x00" + _tlv(0x30, varbind))
    msg = _tlv(0x30, b"\x02\x01\x01" + _tlv(0x04, community.encode()) + pdu)  # SNMP v2c
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(msg, (host, 161))
        data, _ = s.recvfrom(4096)
    except OSError as e:
        raise PrinterError(f"The printer at {host} didn't answer a status request ({e}).") from e
    finally:
        s.close()
    # message -> version, community, response PDU -> id, error, index, varbinds -> varbind -> oid, value
    _, msg_body, _ = _read_tlv(data, 0)
    i = 0
    for _ in range(2):
        _, _, i = _read_tlv(msg_body, i)
    _, pdu_body, _ = _read_tlv(msg_body, i)
    j = 0
    for _ in range(3):
        _, _, j = _read_tlv(pdu_body, j)
    _, vbs, _ = _read_tlv(pdu_body, j)
    _, vb, _ = _read_tlv(vbs, 0)
    _, _, k = _read_tlv(vb, 0)
    tag, value, _ = _read_tlv(vb, k)
    if tag != 0x04:
        raise PrinterError("The printer's status reply was in an unexpected format.")
    return value


def status(host: str) -> dict:
    raw = _snmp_get(host, STATUS_OID)
    if len(raw) < 32 or raw[0] != 0x80 or raw[2] != 0x42:
        raise PrinterError("The printer's status reply wasn't a Brother status block.")
    errors = [m for bit, m in ERRORS_1.items() if raw[8] & bit] + [m for bit, m in ERRORS_2.items() if raw[9] & bit]
    return {
        "model": {0x70: "PT-P950NW", 0x71: "PT-P900", 0x69: "PT-P900W", 0x78: "PT-P910BT"}.get(
            raw[4], f"model {raw[4]:#x}"
        ),
        "errors": errors,
        "tape_mm": raw[10],
        "tape_colour": TAPE_COLOURS.get(raw[24], "other"),
        "laminated": raw[11] == 0x01,
        "busy": raw[18] not in (0x00, 0x01) or raw[19] != 0x00,
        "raw": raw[:32].hex(" "),
    }


COUNTERS_OID = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.10.0"
TOTAL_LENGTH_ID = 0x60  # "Total Print Length (cm)" on the printer's maintenance page


def counter_cm(host: str) -> int | None:
    """Lifetime tape printed, in cm, from the printer's maintenance counters.

    The SNMP value is a list of records: id (1 byte), flag (1), length (1),
    value (big-endian), ending with FFh. Returns None if it can't be read.
    """
    try:
        raw = _snmp_get(host, COUNTERS_OID)
    except PrinterError:
        return None
    i = 0
    while i + 3 <= len(raw) and raw[i] != 0xFF:
        rid, size = raw[i], raw[i + 2]
        value = int.from_bytes(raw[i + 3 : i + 3 + size], "big")
        if rid == TOTAL_LENGTH_ID:
            return value
        i += 3 + size
    return None


def check_tape(st: dict, fmt: str) -> str | None:
    """Why this format can't print on the loaded tape, or None if it can."""
    spec = labels.FORMATS[fmt]
    if st["errors"]:
        return "The printer reports: " + ", ".join(st["errors"]) + "."
    if not st["tape_mm"]:
        return "There's no tape in the printer."
    if st["tape_mm"] != spec["tape_mm"]:
        return (
            f"{spec['name']} tags need {spec['tape_mm']} mm tape ({spec['tape']}); "
            f"the printer has {st['tape_mm']} mm loaded."
        )
    if spec["tape_colour"] != "any" and st["tape_colour"] != spec["tape_colour"]:
        return f"{spec['name']} tags need {spec['tape_colour']} tape; the printer has {st['tape_colour']} tape loaded."
    return None


# ---------- raster encoding ----------


def packbits(data: bytes) -> bytes:
    """TIFF PackBits, as the printer's compression mode 2 expects."""
    out, i, n = bytearray(), 0, len(data)
    while i < n:
        j = i
        while j + 1 < n and data[j + 1] == data[i] and j - i < 127:
            j += 1
        if j > i:  # run of 2..128 identical bytes
            out += bytes([(257 - (j - i + 1)) & 0xFF, data[i]])
            i = j + 1
            continue
        j = i
        while j + 1 < n and data[j + 1] != data[j] and j - i < 127:
            j += 1
        if j + 1 < n and data[j + 1] == data[j]:
            j -= 1  # leave the start of the next run alone
        j = max(j, i)
        out += bytes([j - i]) + data[i : j + 1]
        i = j + 1
    return bytes(out)


def raster_lines(image, tape_mm: int, nudge: int = 0) -> list:
    """One 70-byte line per column of the label (columns run along the tape), last column first."""
    left, area, offset = TAPES[tape_mm]
    # Brother counts the margins from the other end of the head to our bit
    # order, so the print area starts after the *right* margin pins
    # (560 - left - area). Using `left` printed ~1.1 mm low on 12 mm tape.
    base = HEAD_PINS - left - area
    img = image.crop((FEED_DOTS, offset, image.width - FEED_DOTS, offset + area))
    w, h = img.size
    px = img.load()
    lines = []
    # The head prints the first raster line at the far end of the label as
    # read, so send columns right to left (left to right came out mirrored).
    for x in reversed(range(w)):
        bits = bytearray(LINE_BYTES)
        for y in range(h):
            if px[x, y] == 0:  # black = ink
                # `nudge` is a per-printer calibration in dots; negative moves
                # the print down the label, positive moves it up.
                pin = base + nudge + (h - 1 - y)
                if 0 <= pin < HEAD_PINS:
                    bits[pin // 8] |= 0x80 >> (pin % 8)
        lines.append(bytes(bits))
    return lines


MAX_PAGE_DOTS = round(950 / 25.4 * 360)  # keep each strip under the printer's 1000 mm page limit


def join_strip(images: list) -> list:
    """Join labels end to end into strips, with a dashed cut line at each join.

    Returns one image per strip (several only if the batch is over ~950 mm).
    The labels' own blank edges sit either side of each line.
    """
    from PIL import Image, ImageDraw

    strips, current = [], []
    for img in images:
        if current and sum(i.width for i in current) + img.width > MAX_PAGE_DOTS:
            strips.append(current)
            current = []
        current.append(img)
    if current:
        strips.append(current)
    out = []
    for group in strips:
        h = max(i.height for i in group)
        strip = Image.new("1", (sum(i.width for i in group), h), 1)
        x = 0
        d = ImageDraw.Draw(strip)
        for n, img in enumerate(group):
            strip.paste(img, (x, 0))
            if n:
                for y in range(0, h, 12):  # 6 dots on, 6 off, 2 dots wide
                    d.rectangle([x - 1, y, x, min(y + 5, h - 1)], fill=0)
            x += img.width
        out.append(strip)
    return out


def build_job(images: list, tape_mm: int, nudge: int = 0, strip: bool = False) -> bytes:
    """A complete print job, fully cut after every page.

    Normally each label is its own page, so each comes out separately. With
    `strip`, the labels are joined into one long page per strip (see
    join_strip), so the batch comes out in one piece to cut by hand.
    Every job feeds out and cuts after the last page.
    """
    if strip and len(images) > 1:
        images = join_strip(images)
    out = bytearray(b"\x00" * 200 + b"\x1b@")  # invalidate, initialise
    for n, image in enumerate(images):
        lines = raster_lines(image, tape_mm, nudge)
        page = 2 if n == len(images) - 1 else (0 if n == 0 else 1)  # single page counts as last
        out += b"\x1bia\x01"  # raster mode
        out += (
            b"\x1biz" + bytes([0x84, 0x00, tape_mm, 0x00]) + len(lines).to_bytes(4, "little") + bytes([page, 0x00])
        )  # print information
        out += b"\x1biM\x40"  # auto cut
        out += b"\x1biA\x01"  # cut after every page
        # No chain printing (bit 3): chain printing left the last label stuck
        # in the printer. Half cut (bit 2) is not used: on the PT-P950NW it
        # still cut right through, so strips are built as one long page instead.
        out += b"\x1biK\x08"
        out += b"\x1bid" + FEED_DOTS.to_bytes(2, "little")  # 1 mm margins
        out += b"M\x02"  # TIFF compression
        for line in lines:
            if not any(line):
                out += b"Z"
            else:
                packed = packbits(line)
                out += b"G" + len(packed).to_bytes(2, "little") + packed
        out += b"\x1a" if page == 2 else b"\x0c"
    return bytes(out)


def send(host: str, job: bytes, timeout: float = 20.0) -> None:
    with _lock_for(host):
        try:
            with socket.create_connection((host, PORT), timeout=timeout) as s:
                s.sendall(job)
                # Half-close and drain until the printer closes its side, so the
                # whole job is consumed before the connection goes away.
                s.shutdown(socket.SHUT_WR)
                try:
                    while s.recv(4096):
                        pass
                except OSError:
                    pass
        except OSError as e:
            raise PrinterError(f"Couldn't send the job to the printer at {host}: {e}") from e


def wait_done(host: str, timeout: float = 30.0) -> str | None:
    """Poll status until the printer is idle again; returns an error, or None."""
    deadline = time.time() + timeout
    time.sleep(1.5)
    while time.time() < deadline:
        try:
            st = status(host)
        except PrinterError:
            time.sleep(1)
            continue
        if st["errors"]:
            return "The printer reports: " + ", ".join(st["errors"]) + "."
        if not st["busy"]:
            return None
        time.sleep(1)
    return None


# ---------- printers saved in the app ----------


def all_printers(conn) -> list:
    return conn.execute("SELECT * FROM printers ORDER BY is_default DESC, name COLLATE NOCASE, id").fetchall()


def with_status(rows: list) -> list:
    """Each printer row with its live status (or the error), queried in parallel."""
    from concurrent.futures import ThreadPoolExecutor

    def one(row):
        try:
            return {"p": row, "st": status(row["host"]), "err": None}
        except PrinterError as e:
            return {"p": row, "st": None, "err": str(e)}

    if not rows:
        return []
    with ThreadPoolExecutor(max_workers=min(4, len(rows))) as ex:
        return list(ex.map(one, rows))


def route(tags: list, printers: list) -> tuple:
    """Send each label to the first printer (default first) whose tape fits it.

    Returns ({printer id: [tags]}, [tags no printer can take right now]).
    """
    plan, waiting = {}, []
    for t in tags:
        target = next((x for x in printers if x["st"] and check_tape(x["st"], t["format"]) is None), None)
        if target:
            plan.setdefault(target["p"]["id"], []).append(t)
        else:
            waiting.append(t)
    return plan, waiting
