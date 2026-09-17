# -*- coding: utf-8 -*-
"""用 PyQt5 自带的 pyrcc_main 编译 qrc（pyrcc5 console script 在本环境缺失）。

用法: python tools/compile_qrc.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt5 import pyrcc_main

PAIRS = [
    ("icons/icons.qrc", "app/utils/icons_rc.py"),
    ("icons/light/icons_light.qrc", "app/utils/icons_light_rc.py"),
]


def compile_qrc(qrc: str, out: str) -> int:
    """调 pyrcc_main.main()，执行后恢复 sys.argv（pyrcc_main 会读它并 sys.exit）"""
    root = Path(__file__).resolve().parent.parent
    argv = sys.argv
    sys.argv = ["pyrcc5", str(root / qrc), "-o", str(root / out)]
    try:
        pyrcc_main.main()
        print(f"[qrc] {qrc} -> {out}")
        return 0
    except SystemExit as e:
        code = int(e.code or 0)
        if code:
            print(f"[qrc] 失败 {qrc}: exit {code}")
        return code
    finally:
        sys.argv = argv


if __name__ == "__main__":
    rc = 0
    for qrc, out in PAIRS:
        rc |= compile_qrc(qrc, out)
    sys.exit(rc)
