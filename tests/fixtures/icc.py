"""
Минимальный ICC v2 (matrix/TRC) для тестов: в системе нет Display P3, а Pillow создаёт
только sRGB / LAB / XYZ. Первичные цвета — Display P3, адаптированные к D50.
"""

import struct

P3_D50 = {
    "rXYZ": (0.515121, 0.241196, -0.001053),
    "gXYZ": (0.291977, 0.692245, 0.041885),
    "bXYZ": (0.157104, 0.066574, 0.784073),
    "wtpt": (0.964203, 1.0, 0.824905),
}


def _s15(value: float) -> bytes:
    return struct.pack(">i", round(value * 65536))


def _xyz(values) -> bytes:
    return b"XYZ \x00\x00\x00\x00" + b"".join(_s15(v) for v in values)


def _curve(gamma: float) -> bytes:
    return b"curv\x00\x00\x00\x00" + struct.pack(">IH", 1, round(gamma * 256)) + b"\x00\x00"


def _desc(text: str) -> bytes:
    ascii_ = text.encode("ascii") + b"\x00"
    return b"desc\x00\x00\x00\x00" + struct.pack(">I", len(ascii_)) + ascii_ + struct.pack(">II", 0, 0) + b"\x00\x00\x00" + b"\x00" * 67


def matrix_profile(description: str = "Display P3", primaries: dict = P3_D50, gamma: float = 2.2) -> bytes:
    tags = [(b"desc", _desc(description))]
    tags += [(name.encode(), _xyz(values)) for name, values in primaries.items()]
    curve = _curve(gamma)
    tags += [(b"rTRC", curve), (b"gTRC", curve), (b"bTRC", curve)]

    offset = 128 + 4 + 12 * len(tags)
    table, data = b"", b""
    for signature, payload in tags:
        payload += b"\x00" * (-len(payload) % 4)
        table += signature + struct.pack(">II", offset + len(data), len(payload))
        data += payload
    body = struct.pack(">I", len(tags)) + table + data

    size = 128 + len(body)
    header = struct.pack(">I", size) + b"\x00" * 4 + struct.pack(">I", 0x02100000) + b"mntrRGB XYZ "
    header += b"\x00" * 12 + b"acsp" + b"\x00" * 24 + struct.pack(">I", 0)
    header += _s15(0.9642) + _s15(1.0) + _s15(0.8249) + b"\x00" * 48
    assert len(header) == 128
    return header + body
