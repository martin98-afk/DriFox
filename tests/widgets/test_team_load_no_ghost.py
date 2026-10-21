# -*- coding: utf-8 -*-
"""回归测试：/team --load 不得冒出"幽灵窗口"（独立 ToolPopupDialog）。

设计说明：
- 本仓库 main_widget.py 等模块存在历史遗留的 Python2 风格 `except A, B:`
  语法（约 20 处），导致整文件 `ast.parse` / `import` 在 Python3 下直接失败。
  因此本测试**不解析、不导入**模块，仅以纯文本方式校验关键方法的源码，
  避免被无关的语法错误阻塞，同时仍然能在 CI 修复 Py2 语法后持续生效。

核心断言：
1. `_create_fresh_window` 与 `_duplicate_window` 不再实例化 `ToolPopupDialog`，
   而是统一路由到 `TabManagerWindow.add_window`（多窗口模式已下线）。
2. gitee 卡片与 LLM 设置卡片中用于切换至多窗口模式的开关回调已被移除，
   从源头消除状态不一致。
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _method_body(src: str, method_name: str) -> str:
    """按缩进提取方法体（不解析整个模块，规避无关语法错误）。"""
    lines = src.splitlines()
    start = None
    indent = None
    for i, line in enumerate(lines):
        m = re.match(rf"^(\s*)def {method_name}\(", line)
        if m:
            start = i
            indent = len(m.group(1))
            break
    if start is None:
        raise AssertionError(f"未找到方法 {method_name}")

    body = []
    for j in range(start, len(lines)):
        if j == start:
            body.append(lines[j])
            continue
        cur = lines[j]
        if cur.strip() == "":
            body.append(cur)
            continue
        cur_indent = len(cur) - len(cur.lstrip())
        if cur_indent <= indent and re.match(r"^\s*(def |class )", cur):
            break
        body.append(cur)
    return "\n".join(body)


def test_create_fresh_window_routes_to_tab_manager():
    """_create_fresh_window 必须加入 TabManagerWindow，禁止降级为独立弹窗。"""
    src = (REPO_ROOT / "app" / "main_widget.py").read_text(encoding="utf-8")
    body = _method_body(src, "_create_fresh_window")
    # ASCII 圆括号的 ToolPopupDialog( 仅出现在真正的实例化处（注释用全角括号，不会误判）
    assert "ToolPopupDialog(" not in body, (
        "幽灵窗口源未消除：_create_fresh_window 仍会创建独立 ToolPopupDialog"
    )
    assert "TabManagerWindow" in body, (
        "_create_fresh_window 应路由到 TabManagerWindow"
    )
    assert "add_window" in body, (
        "_create_fresh_window 应调用 TabManagerWindow.add_window"
    )


def test_duplicate_window_routes_to_tab_manager():
    """_duplicate_window 同样必须加入 TabManagerWindow。"""
    src = (REPO_ROOT / "app" / "main_widget.py").read_text(encoding="utf-8")
    body = _method_body(src, "_duplicate_window")
    assert "ToolPopupDialog(" not in body, (
        "幽灵窗口源未消除：_duplicate_window 仍会创建独立 ToolPopupDialog"
    )
    assert "TabManagerWindow" in body
    assert "add_window" in body


def test_gitee_card_tab_toggle_removed():
    """gitee 卡片中切换多窗口模式的回调应已移除（多窗口模式已下线）。"""
    src = (REPO_ROOT / "app" / "widgets" / "cards" / "settings" / "gitee_card.py").read_text(
        encoding="utf-8"
    )
    assert "def _on_tab_toggled" not in src, "gitee 卡片仍存在 _on_tab_toggled，多窗口切换未被下线"
    assert "def _do_tab_toggle" not in src, "gitee 卡片仍存在 _do_tab_toggle，多窗口切换未被下线"


def test_llm_settings_card_tab_toggle_removed():
    """LLM 设置卡片中切换多窗口模式的回调应已移除。"""
    src = (
        REPO_ROOT / "app" / "widgets" / "cards" / "settings" / "llm_settings_card.py"
    ).read_text(encoding="utf-8")
    assert "def _on_tab_manager_toggled" not in src, (
        "LLM 设置卡片仍存在 _on_tab_manager_toggled，多窗口切换未被下线"
    )
