# -*- coding: utf-8 -*-
"""回归守卫：history-manager 的 _active_window() 必须返回窗口对象（非快照 dict）。

背景（2026-10-18 真实回归）：迁移时把 `_active_window()` 改成走
window.current（语义快照 dict），但 9 个调用点做的是
`win._on_history_session_selected(index)` 之类的方法调用 → 运行时
AttributeError: 'dict' object has no attribute '_on_history_session_selected'
表现：历史会话切换项目失效。

只想要语义字段时用 window.current；要窗口对象时用 window.active。
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HISTORY_PAGE = REPO / "plugins" / "history-manager" / "ui" / "history_page.py"


def test_active_window_uses_window_active_capability():
    """_active_window 必须走 window.active（返回对象），不得用 window.current。"""
    src = HISTORY_PAGE.read_text(encoding="utf-8")
    idx = src.find("def _active_window")
    assert idx > 0, "找不到 _active_window"
    end = len(src)
    for marker in ("\ndef ", "\nclass "):
        k = src.find(marker, idx + 10)
        if k > 0 and k < end:
            end = k
    body = src[idx:end]
    # 代码里必须走 window.active（docstring 里的说明文字不算，故看调用形式）
    assert '_caps("window.active")' in body, "_active_window 必须走 window.active"
    assert (
        'window.current' not in body.replace('只想要语义字段（project/workdir/session）时用 window.current。', '')
    ), "_active_window 不得返回语义快照"


def test_no_snapshot_dict_used_as_window():
    """全仓扫描：window.current 的结果不得当对象调方法。

    判据：变量的值直接来自 _caps("window.current") 的调用结果（snap = ...()），
    随后出现 <var>._xxx( 的调用 —— dict 没有下划线方法名，必然是误用。
    """
    SKIP = {"__pycache__", "deps", "skills", "examples"}
    bad = []
    for path in sorted((REPO / "plugins").rglob("*.py")):
        rel = path.relative_to(REPO / "plugins")
        if any(p in SKIP for p in rel.parts):
            continue
        src = path.read_text(encoding="utf-8", errors="ignore")
        if "window.current" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue

        # 1) _caps("window.current") 赋给谁
        cap_vars: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                seg = ast.get_source_segment(src, node.value) or ""
                if 'window.current' in seg:
                    cap_vars.add(node.targets[0].id)

        # 2) <cap_var>() 的结果赋给谁 → 那是快照变量
        snap_vars: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                seg = ast.get_source_segment(src, node.value) or ""
                # 形如：snap = snap() / snap = _caps(...)() / a = active()
                if any(f"{v}()" in seg.replace(" ", "") for v in cap_vars) or (
                    seg.strip().endswith("()") and seg.strip().startswith(tuple(cap_vars))
                ):
                    snap_vars.add(node.targets[0].id)
        # cap 变量若被就地重绑（snap = snap()），它自己也成了快照
        snap_vars |= {v for v in cap_vars if v in snap_vars}

        # 3) 快照变量上不得出现 ._xxx() 调用
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr.startswith("_")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in snap_vars
            ):
                bad.append(f"{path.name}:{node.lineno} {node.func.value.id}.{node.func.attr}()")
    assert not bad, f"语义快照被当窗口对象用: {bad}"
