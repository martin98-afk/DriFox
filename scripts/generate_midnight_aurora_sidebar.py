"""生成“午夜极光”主题使用的左侧竖向背景图。"""

from __future__ import annotations

import argparse
import binascii
import struct
import zlib
from pathlib import Path

Color = tuple[int, int, int]
GRADIENT_START = (25, 12, 58)
GRADIENT_END = (185, 154, 221)


def interpolate_color(position: float) -> Color:
    """在深紫和薰衣草色之间插值，并限制结果在 0 到 1 之间。"""
    position = min(1.0, max(0.0, position))
    return tuple(int(start + (end - start) * position) for start, end in zip(GRADIENT_START, GRADIENT_END))


def _clamp(value: float) -> int:
    """将颜色通道值限制到 PNG 的 8 位范围。"""
    return max(0, min(255, int(round(value))))


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    """构造带长度和 CRC 的 PNG 数据块。"""
    checksum = binascii.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def build_png(width: int, height: int, pixels: bytes) -> bytes:
    """把 RGBA 像素编码为标准 PNG。"""
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(pixels, 9))
        + _png_chunk(b"IEND", b"")
    )


def generate_sidebar_png(output: Path, width: int = 480, height: int = 1920) -> None:
    """生成一张单色紫色水平渐变的 RGBA 极光图。"""
    if width < 2 or height < 2:
        raise ValueError("width and height must be at least 2")

    rows = bytearray()
    for _y in range(height):
        rows.append(0)  # 每行使用 PNG 过滤器类型 0。
        for x in range(width):
            horizontal = x / (width - 1)
            red, green, blue = interpolate_color(horizontal)

            # 两侧逐渐透明，让渐变图与主题的深色底色自然融合。
            edge = min(1.0, horizontal / 0.12, (1.0 - horizontal) / 0.12)
            edge = max(0.0, min(1.0, edge))
            alpha = _clamp(255 * (0.28 + 0.72 * edge))
            rows.extend((red, green, blue, alpha))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(build_png(width, height, bytes(rows)))


def main() -> int:
    """解析命令行参数并生成背景图。"""
    parser = argparse.ArgumentParser(description="生成午夜极光左侧竖向背景图")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=480)
    parser.add_argument("--height", type=int, default=1920)
    args = parser.parse_args()
    generate_sidebar_png(args.output, args.width, args.height)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
