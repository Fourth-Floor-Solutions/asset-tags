from app import codes


def test_round_trip():
    for n in (0, 1, 31, 32, 12345678, codes.SPACE - 1):
        assert codes.to_num(codes.to_code(n)) == n


def test_lookalikes_resolve():
    assert codes.parse("7kq3o") == "7KQ30"
    assert codes.parse("1l1i0") == "11110"
    assert codes.parse("7kq-3m") == "7KQ3M"


def test_rejects_u_and_wrong_length():
    assert codes.parse("7KQ3U") is None
    assert codes.parse("7KQ3") is None
    assert codes.parse("login") == "10G1N"  # valid shape; explicit routes win over the scan route
