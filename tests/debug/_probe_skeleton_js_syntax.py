# -*- coding: utf-8 -*-
"""骨架 JS 语法校验：把真实 _load_skeleton 产物里的 <script> 全部用 node 编译。

背景：骨架 JS 是 f-string 拼出来的，一处括号错配即整串 SyntaxError，
runJavaScript 静默丢弃（无日志、DOM 不变）。改骨架后必须编译真实产物。

用法：python tests/debug/_probe_skeleton_js_syntax.py
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("DRIFOX_DATA_DIR", str(_ROOT / ".drifox-test"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["DRIFOX_QT_RENDERER"] = "1"

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from app.widgets.card_viewers import CodeWebViewer  # noqa: E402

def build_skeleton():
    v = CodeWebViewer.__new__(CodeWebViewer)
    v._light_skeleton = True
    v._is_js_ready = False
    v._v2_last = None
    v._history_collapse_applied = False
    v._v2_live_key = ""
    v._v2_live_sent = ""
    v._v2_live_formalized_len = 0
    v._v2_live_formalize_ts = 0.0
    v._dock_sync_requested = False
    v._streaming = True
    captured = {}

    def _set_html(html, base=None):
        captured["html"] = html

    v.setHtml = _set_html
    v.ensure_transparent_composition = lambda: None
    CodeWebViewer._load_skeleton(v)
    return captured.get("html", "")

def main():
    html = build_skeleton()
    print(f"skeleton html: {len(html)} chars")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    print(f"scripts: {len(scripts)}")
    tmp = Path(tempfile.mkdtemp(prefix="skeleton_js_"))
    bad = 0
    for i, js in enumerate(scripts):
        f = tmp / f"s{i}.js"
        f.write_text(js, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        if r.returncode != 0:
            bad += 1
            print(f"[FAIL] script #{i} ({len(js)} chars)\n{r.stderr[:2000]}")
        else:
            print(f"[ok] script #{i} ({len(js)} chars)")
    print("RESULT:", "FAIL" if bad else "OK")
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
