import hashlib
import struct
import subprocess
import sys
import zlib
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "generate_midnight_aurora_sidebar.py"


def read_png_rgba(path: Path) -> tuple[int, int, list[bytes]]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    width, height, bit_depth, color_type = struct.unpack(">IIBB", data[16:26])
    assert (bit_depth, color_type) == (8, 6)

    offset = 8
    compressed = bytearray()
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        if kind == b"IDAT":
            compressed.extend(payload)
        offset += length + 12

    raw = zlib.decompress(compressed)
    stride = width * 4
    rows = []
    for row_index in range(height):
        start = row_index * (stride + 1)
        assert raw[start] == 0
        rows.append(raw[start + 1 : start + 1 + stride])
    return width, height, rows


def test_generate_sidebar_png_is_single_purple_gradient_and_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    command = [sys.executable, str(SCRIPT), "--output", str(first), "--width", "32", "--height", "64"]
    subprocess.run(command, check=True)
    subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(second), "--width", "32", "--height", "64"],
        check=True,
    )

    width, height, rows = read_png_rgba(first)
    second_width, second_height, second_rows = read_png_rgba(second)
    assert (width, height) == (32, 64)
    assert (second_width, second_height) == (32, 64)
    assert tuple(rows[0][0:3]) == (25, 12, 58)
    assert tuple(rows[0][-4:-1]) == (185, 154, 221)
    center_offset = (width // 2) * 4
    assert all(row[center_offset : center_offset + 3] == rows[0][center_offset : center_offset + 3] for row in rows)
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
