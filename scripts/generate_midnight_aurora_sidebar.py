"""生成“午夜极光”主题使用的左侧竖向背景图。"""

from __future__ import annotations

import argparse
import binascii
import math
import struct
import zlib
from pathlib import Path

Color = tuple[int, int, int]

# 水平方向的基础色带：靛蓝 -> 紫罗兰 -> 品红 -> 青绿 -> 深海蓝。
COLOR_STOPS = (
    (0.00, (7, 11, 38)),
    (0.22, (48, 30, 112)),
    (0.43, (176, 43, 157)),
    (0.68, (14, 167, 157)),
    (0.86, (39, 74, 139)),
    (1.00, (8, 14, 43)),
)


def interpolate_color(position: float) -> Color:
    """在相邻色标之间插值，并限制结果在 0 到 1 之间。"""
    position = min(1.0, max(0.0, position))
    for (left_position, left_color), (right_position, right_color) in zip(
        COLOR_STOPS,
        COLOR_STOPS[1:],
    ):
        if position <= right_position:
            ratio = (position - left_position) / (right_position - left_position)
            return tuple(int(left + (right - left) * ratio) for left, right in zip(left_color, right_color))
    return COLOR_STOPS[-1][1]


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
    """生成一张具有竖向流动光带的 RGBA 极光图。"""
    if width < 2 or height < 2:
        raise ValueError("width and height must be at least 2")

    rows = bytearray()
    for y in range(height):
        vertical = y / (height - 1)
        rows.append(0)  # 每行使用 PNG 过滤器类型 0。
        for x in range(width):
            horizontal = x / (width - 1)
            red, green, blue = interpolate_color(horizontal)

            flow = 0.5 + 0.5 * math.sin(vertical * math.tau * 3.0 + math.sin(horizontal * math.tau) * 0.7)
            violet_band = math.exp(-((horizontal - (0.28 + 0.035 * math.sin(vertical * math.tau * 2.0))) ** 2) / 0.018)
            teal_band = math.exp(
                -((horizontal - (0.67 + 0.045 * math.sin(vertical * math.tau * 1.4 + 1.0))) ** 2) / 0.022
            )
            magenta_band = math.exp(-((horizontal - (0.46 + 0.025 * math.sin(vertical * math.tau * 2.6))) ** 2) / 0.012)

            red = _clamp(red + 38 * violet_band + 26 * magenta_band + 8 * flow)
            green = _clamp(green + 36 * teal_band + 12 * violet_band)
            blue = _clamp(blue + 42 * teal_band + 30 * violet_band)

            # 两侧逐渐透明，让极光条与主题的深色底色自然融合。
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
