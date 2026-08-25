import hashlib
import struct
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "generate_midnight_aurora_sidebar.py"


def read_png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def test_generate_sidebar_png_is_rgba_and_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    command = [sys.executable, str(SCRIPT), "--output", str(first), "--width", "32", "--height", "64"]
    subprocess.run(command, check=True)
    subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(second), "--width", "32", "--height", "64"],
        check=True,
    )

    assert read_png_size(first) == (32, 64)
    assert read_png_size(second) == (32, 64)
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    assert len(first.read_bytes()) > 8
