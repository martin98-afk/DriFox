# -*- coding: utf-8 -*-
"""用 pyside6-rcc 编译 qrc 资源（PySide6 分支专用）。

PyQt5 的 pyrcc5 产物是 `from PyQt5 import QtCore`，PySide6 环境下 import 直接失败
→ 资源未注册 → 全量图标 fallback 成 FluentIcon.APPLICATION（表现为「图标一大堆
加载不出来」）。本分支必须用 pyside6-rcc 重新生成。

用法: python tools/compile_qrc.py [--check]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PAIRS = [
    ("icons/icons.qrc", "app/utils/icons_rc.py"),
    ("icons/light/icons_light.qrc", "app/utils/icons_light_rc.py"),
]


def rcc_available() -> str:
    exe = Path(sys.executable).parent / "pyside6-rcc.exe"
    if exe.exists():
        return str(exe)
    cmd = Path(sys.executable).parent / "pyside6-rcc"
    if cmd.exists():
        return str(cmd)
    raise SystemExit("找不到 pyside6-rcc（应在 .venv/Scripts 下，pip 安装 PySide6 自带）")


def compile_qrc(rcc: str, qrc: str, out: str) -> int:
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([rcc, str(root / qrc), "-o", str(root / out)], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[qrc] {qrc} 失败: {r.stderr[:400]}")
        return r.returncode
    head = (root / out).read_text(encoding="utf-8", errors="replace")[:400]
    if "from PyQt5" in head:
        print(f"[qrc] {qrc} 产物仍引用 PyQt5，检查 rcc 版本")
        return 1
    print(f"[qrc] {qrc} -> {out}")
    return 0


def main() -> int:
    if "--check" in sys.argv:
        root = Path(__file__).resolve().parent.parent
        bad = [
            out
            for _qrc, out in PAIRS
            if "from PyQt5" in (root / out).read_text(encoding="utf-8", errors="replace")[:400]
        ]
        if bad:
            print(f"[qrc] 以下产物为 PyQt5 产物，需重新编译: {bad}")
            return 1
        print("[qrc] 产物均为 PySide6 格式")
        return 0

    rcc = rcc_available()
    for qrc, out in PAIRS:
        code = compile_qrc(rcc, qrc, out)
        if code:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
