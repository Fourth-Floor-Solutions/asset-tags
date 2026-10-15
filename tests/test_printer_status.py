"""Brother status block over SNMP, decoded from a hand-built SNMP response."""

from app import printer


def _snmp_response(value: bytes) -> bytes:
    def tlv(tag, body):
        return bytes([tag]) + printer._ber_len(len(body)) + body

    varbind = tlv(0x30, printer._oid(printer.STATUS_OID) + tlv(0x04, value))
    pdu = tlv(0xA2, b"\x02\x01\x01\x02\x01\x00\x02\x01\x00" + tlv(0x30, varbind))
    return tlv(0x30, b"\x02\x01\x01" + tlv(0x04, b"public") + pdu)


def _status_block(width=18, colour=0x06, err1=0, err2=0):
    raw = bytearray(32)
    raw[0], raw[1], raw[2], raw[4] = 0x80, 0x20, 0x42, 0x70
    raw[8], raw[9], raw[10], raw[11], raw[24] = err1, err2, width, 0x01, colour
    return bytes(raw)


class _FakeSocket:
    reply = b""

    def __init__(self, *a, **k):
        pass

    def settimeout(self, t):
        pass

    def sendto(self, msg, addr):
        self.sent = msg

    def recvfrom(self, n):
        return self.reply, ("x", 161)

    def close(self):
        pass


def test_status_decodes_tape_and_errors(monkeypatch):
    monkeypatch.setattr(printer, "_snmp_get", printer._snmp_get_real)  # real decoder, fake socket
    _FakeSocket.reply = _snmp_response(_status_block(width=12, colour=0x04, err2=0x10))
    monkeypatch.setattr(printer.socket, "socket", _FakeSocket)
    st = printer.status("192.0.2.1")
    assert st["model"] == "PT-P950NW" and st["tape_mm"] == 12 and st["tape_colour"] == "red"
    assert st["errors"] == ["Cover open"] and st["laminated"]


def test_check_tape_reports_errors_first():
    st = {"errors": ["Cover open"], "tape_mm": 18, "tape_colour": "yellow"}
    assert "Cover open" in printer.check_tape(st, "standard")
    assert "no tape" in printer.check_tape({"errors": [], "tape_mm": 0, "tape_colour": "other"}, "standard")


def test_packbits_runs_and_literals():
    data = bytes([0] * 20 + [0x22, 0x22, 0x23, 0xBA, 0xBF, 0xA2, 0x22, 0x2B])
    packed = printer.packbits(data)
    assert packed[:2] == bytes([0xED, 0x00])  # 20 zeros as one run, as in Brother's example
