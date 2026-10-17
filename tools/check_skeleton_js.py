"""骨架 JS 语法自检（离线，不需要 Chromium / GPU）。

套路（见 .workbuddy/memory/topics/webview-rendering.md「校验套路」）：
  1. 用 AST 取 `CodeWebViewer._load_skeleton` 里的骨架 f-string，把 `{expr}`
     占位替换成空串（f-string 的字面量部分原样保留，`{{`→`{` 由 AST 自动还原）；
  2. 抽出所有 <script> 块，逐个跑 `node --check`；
  3. 额外把骨架注入的 JS 资产（_STREAMING_DOCK_JS / _BODY_HEIGHT_CAP_JS /
     _TYPEWRITER_JS / _FLIP_JS ...）单独拼成脚本再 check。

用法：.venv/Scripts/python.exe tools/check_skeleton_js.py
"""

from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "app", "widgets", "card_viewers.py")
NODE = r"C:\Users\black\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"


def _joined_str_text(node: ast.AST) -> str:
    """把 JoinedStr（可能由隐式拼接组成）摊平成「字面量 + 占位」文本。"""
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(str(v.value))
            else:
                out.append("0")  # 表达式占位：填 0 可让"注入值位置"仍是合法 JS（空串会造出 `= ;`）
        return "".join(out)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def extract_skeleton(fn_name: str = "_load_skeleton") -> str:
    src = io.open(TARGET, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fn_name:
            best = ""
            for sub in ast.walk(node):
                if isinstance(sub, (ast.JoinedStr, ast.Constant)):
                    txt = _joined_str_text(sub)
                    if len(txt) > len(best):
                        best = txt
            return best
    return ""


def check_js(code: str, tag: str) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(code)
        path = f.name
    try:
        r = subprocess.run([NODE, "--check", path], capture_output=True, text=True, encoding="utf-8", errors="replace")
        ok = r.returncode == 0
        print(f"[{'OK ' if ok else 'FAIL'}] {tag} ({len(code)} chars)")
        if not ok:
            print((r.stderr or r.stdout)[:1500])
        return ok
    finally:
        os.unlink(path)


def main() -> int:
    html = extract_skeleton()
    if not html:
        print("骨架抽取失败")
        return 1
    print(f"骨架 HTML 抽取 {len(html)} 字符")
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    print(f"<script> 块 {len(scripts)} 个")
    ok = True
    for i, js in enumerate(scripts):
        if js.strip():
            ok &= check_js(js, f"skeleton<script>#{i}")

    # 注入的 JS 资产单独校验（骨架里被替换成空串，需补测）
    sys.path.insert(0, ROOT)
    import app.widgets.card_render_core as crc  # noqa: E402

    for name in (
        "_STREAMING_DOCK_JS",
        "_BODY_HEIGHT_CAP_JS",
        "_TYPEWRITER_JS",
        "_FLIP_JS",
        "_CONTENT_AUTOSCROLL_JS",
    ):
        asset = getattr(crc, name, None)
        if isinstance(asset, str) and asset.strip():
            ok &= check_js(asset, f"asset:{name}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
