# -*- coding: utf-8 -*-
"""逐帧行投影分析：检测录屏中的文字竖向拉伸帧（零依赖，读 PGM）。

判据：正常帧文字行厚（单行文字的垂直厚度）≈ 基线；拉伸帧行厚显著放大。
"""

import glob
import os
import sys


def read_pgm(path: str):
    with open(path, "rb") as f:
        data = f.read()
    # P5 二进制，头部分词跨行兼容：P5 <w> <h> <max> 之后是原始字节
    idx, tokens = 2, []
    while len(tokens) < 3:
        while idx < len(data) and data[idx : idx + 1].isspace():
            idx += 1
        if data[idx : idx + 1] == b"#":  # 注释行
            while data[idx : idx + 1] not in (b"\n", b""):
                idx += 1
            continue
        start = idx
        while idx < len(data) and not data[idx : idx + 1].isspace():
            idx += 1
        tokens.append(int(data[start:idx]))
    w, h = tokens[0], tokens[1]
    return data[idx + 1 : idx + 1 + w * h], w, h


def band_stats(path: str):
    raw, w, h = read_pgm(path)
    dark = 100
    profile = [sum(1 for x in range(w) if raw[y * w + x] < dark) for y in range(h)]
    bands, start = [], None
    for i, v in enumerate(profile):
        on = v > 0
        if on and start is None:
            start = i
        elif not on and start is not None:
            if i - start >= 2:
                bands.append((start, i))
            start = None
    if start is not None:
        bands.append((start, h))
    thick = sorted(e - s for s, e in bands)
    med = thick[len(thick) // 2] if thick else 0
    return len(bands), med


def main() -> None:
    frames = sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "frames", "*.pgm")))
    if not frames:
        print("no frames")
        return
    stats = []
    for f in frames:
        n, med = band_stats(f)
        stats.append((os.path.basename(f), n, med))
    goods = [s[2] for s in stats if s[2] > 0]
    base = sorted(goods)[len(goods) // 2] if goods else 1
    print(f"frames={len(stats)} baseline_band_thickness={base}px")
    stretch = 0
    for name, n, thick in stats:
        flag = "STRETCH" if thick > base * 18 // 10 and thick > 6 else ""
        if flag:
            stretch += 1
            print(f"{name}  bands={n}  thick={thick}  {flag}")
    print(f"STRETCH_FRAMES={stretch}/{len(stats)}")


if __name__ == "__main__":
    sys.exit(main())
