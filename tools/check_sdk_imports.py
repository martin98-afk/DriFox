"""校验改动文件里 from app.plugins.sdk import 的符号都真实存在。

背景：本轮批量把越界 import 改成 sdk，出现过「改了 import 行但漏改使用处」
（_detectors.py 的 detect_provider_family）与「sdk 没收录就 import」两类错。
编译期抓不到（PEP 562 __getattr__ 运行时才抛），故静态核对一遍。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def changed_py_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    files = [f for f in out if f.endswith(".py")]
    extra = [
        "app/plugins/capabilities/resolve.py",
        "tests/plugins/test_sdk_stability_contract.py",
    ]
    for e in extra:
        if (ROOT / e).is_file() and e not in files:
            files.append(e)
    return files


def sdk_symbols() -> set[str]:
    """从 sdk.py 的 _SYMBOLS 字面量里静态取出全部键（不 import，避免拉 Qt）。"""
    src = (ROOT / "app/plugins/sdk.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        value = None
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "_SYMBOLS":
            value = node.value
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_SYMBOLS":
                    value = node.value
                    break
        if value is None:
            continue
        keys = set()
        for k in value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
        if keys:
            return keys
    return set()


def main() -> int:
    known = sdk_symbols()
    problems = []
    for rel in changed_py_files():
        path = ROOT / rel
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="ignore"))
        except SyntaxError as e:
            problems.append(f"{rel}: 语法错误 {e}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != "app.plugins.sdk":
                continue
            for alias in node.names:
                name = alias.name
                if name == "*":
                    continue
                if name not in known:
                    problems.append(f"{rel}:{node.lineno} from app.plugins.sdk import {name} 未收录")
    if problems:
        print("[sdk-import-check] 发现问题：")
        for p in problems:
            print("  " + p)
        return 1
    print(f"[sdk-import-check] OK（{len(known)} 个 sdk 符号，改动文件全部命中）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
