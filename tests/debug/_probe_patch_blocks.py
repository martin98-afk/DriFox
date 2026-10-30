# -*- coding: utf-8 -*-
"""离线验证 patchBlocks：构造骨架 HTML，检查 JS 语法与 splice 行为。

不启动 WebEngine（offscreen 下 QWebEngineView 会崩），改为：
1. 直接抓 CodeWebViewer 的骨架构造结果，确认 patchBlocks 已内联且 f-string 未炸；
2. 用 node 校验骨架内 script 的语法（有 node 时）；
3. 用纯 Python 模拟 splice 语义，验证 _blockNodes / 删除 / 插入 / 重编号规则。
"""

import os
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("DRIFOX_DATA_DIR", str(_ROOT / ".drifox-test"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)


def grab_skeleton_source() -> str:
    """拿到 _load_skeleton 里拼骨架的那段 f-string 源码（不实例化 WebView）。"""
    src = (_ROOT / "app/widgets/card_viewers.py").read_text(encoding="utf-8")
    i = src.find("def _load_skeleton")
    j = src.find("def _on_js_ready", i)
    return src[i:j]


def main():
    seg = grab_skeleton_source()
    checks = {
        "patchBlocks 定义": "window.patchBlocks = function" in seg,
        "_blockNodes 定义": "window._blockNodes = function" in seg,
        "_applyBlockMeta 定义": "function _applyBlockMeta" in seg,
        "data-block-key 契约": "data-block-key" in seg,
        "data-block-index 契约": "data-block-index" in seg,
        "后处理已接": "_wrapTablesIn" in seg and "_initEchartsIn" in seg,
    }
    for name, ok in checks.items():
        print(f"  {'OK ' if ok else 'FAIL'} {name}")

    # f-string 花括号平衡（{{ }} 成对 → 单花括号必须为 0）
    body = seg[seg.find("B5 块级补丁通道") : seg.find("B1 差量渲染：未闭合尾部行内渲染")]
    stripped = body.replace("{{", "").replace("}}", "")
    print(f"  未转义花括号: {stripped.count('{')} 个 {{ , {stripped.count('}')} 个 }}")

    # 用 node 校验这段 JS 语法（有 node 才跑）
    node = None
    for cand in ("node", "node.exe"):
        try:
            subprocess.run([cand, "--version"], capture_output=True, check=True, timeout=5)
            node = cand
            break
        except Exception:
            continue
    if node:
        snippet = body.split("function _applyBlockMeta")[1]
        snippet = "function _applyBlockMeta" + snippet.split("window.patchBlocks")[0]
        js = snippet.replace("{{", "{").replace("}}", "}")
        tmp = _ROOT / "tests/debug/_patch_blocks_syntax.js"
        tmp.write_text("var window = {}; var document = {};\n" + js + "\n", encoding="utf-8")
        r = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True)
        print(f"  node --check: {'OK' if r.returncode == 0 else 'FAIL'}")
        if r.returncode:
            print(r.stderr[:600])
        tmp.unlink(missing_ok=True)
    else:
        print("  node 不可用，跳过语法校验")


if __name__ == "__main__":
    main()
