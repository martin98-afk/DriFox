# -*- coding: utf-8 -*-
"""编译 qrc 资源（PySide6 用 pyside6-rcc 命令行；PyQt5 的 pyrcc_main 无 PySide6 等效内嵌 API）。
用法: python tools/compile_qrc.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PAIRS = [
    ("icons/icons.qrc", "app/utils/icons_rc.py"),
    ("icons/light/icons_light.qrc", "app/utils/icons_light_rc.py"),
]


def compile_qrc(qrc: str, out: str) -> int:
    """调 pyside6-rcc 编译单个 qrc → _rc.py"""
    root = Path(__file__).resolve().parent.parent
    cmd = [sys.executable, "-m", "PySide6.scripts.pyside_tool", "rcc", str(root / qrc), "-o", str(root / out)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        print(f"[qrc] 未找到 pyside6-rcc（PySide6 未安装？）: {qrc}")
        return 1
    if r.returncode:
        print(f"[qrc] 失败 {qrc}: exit {r.returncode}\n{r.stderr[:500]}")
        return r.returncode
    print(f"[qrc] {qrc} -> {out}")
    return 0


if __name__ == "__main__":
    rc = 0
    for qrc, out in PAIRS:
        rc |= compile_qrc(qrc, out)
    sys.exit(rc)
