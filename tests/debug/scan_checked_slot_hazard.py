# -*- coding: utf-8 -*-
"""扫描：带默认参的 slot 被 triggered/clicked(bool) 信号直连的隐患点。

QAction.triggered / QAbstractButton.clicked 都带一个 bool 参数，直接
connect 到「第一个形参带默认值」的 Python slot 时，默认值会被 checked
实参覆盖 —— 默认 True 的开关型参数会被静默翻成 False。
"""

import os
import re

ROOT = "app"

SLOT_DEF = re.compile(r"def\s+(\w+)\s*\(\s*self\s*,\s*\w+\s*:\s*\w+\s*=\s*(?:True|False)\s*\)")
CONNECT = re.compile(r"(triggered|clicked)\.connect\(\s*self\.(\w+)\s*\)")
SELF_CONNECT = re.compile(r"self\.(\w+)\.connect\(\s*self\.(\w+)\s*\)")


def main() -> int:
    hits = []
    for dp, dn, fn in os.walk(ROOT):
        if "__pycache__" in dp:
            continue
        for f in fn:
            if not f.endswith(".py"):
                continue
            p = os.path.join(dp, f)
            t = open(p, encoding="utf-8", errors="ignore").read()
            defaults = {m.group(1): m.group(0).strip() for m in SLOT_DEF.finditer(t)}
            if not defaults:
                continue
            for m in CONNECT.finditer(t):
                name = m.group(2)
                if name in defaults:
                    ln = t[: m.start()].count("\n") + 1
                    hits.append((p, ln, m.group(1), defaults[name]))
    for p, ln, sig, d in hits:
        print(f"{p}:{ln}  [{sig}] -> {d}")
    print("总计", len(hits))
    return len(hits)


if __name__ == "__main__":
    raise SystemExit(main())
