# -*- coding: utf-8 -*-
"""回归测试：保存当前会话后必须刷新历史面板 UI（避免列表停留在保存前快照）。

== 问题描述 ==
三个保存路径都未触发历史面板 UI 刷新：
1. `_save_current_session_to_history` — 流式完成后保存消息
2. `_auto_save_current_session` — 关闭窗口/项目切换等自动保存
3. `_create_new_session` — 内部 `_auto_save_current_session` 保存旧会话

当历史卡片已展开时，用户继续对话/建新会话，列表停留在保存前的快照，
需关闭/重新打开面板才能看到新会话。

== 修复 ==
在三处末尾追加：
    refresh_history_card_if_visible(self._history_card, self._refresh_history_toggle_panel)
`refresh_history_card_if_visible` 内部已判 `isVisible()`，不可见时 0 开销。

== 回归测试 ==
- AST 静态检查：三个方法末尾必须包含上述调用（防回归）
- 调用级验证：用 mock 替换 `refresh_history_card_if_visible`，验证被以正确参数调用
"""

import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_PATH = _REPO_ROOT / "app" / "main_widget.py"


# 三处保存路径
TARGET_METHODS = (
    "_save_current_session_to_history",
    "_auto_save_current_session",
    "_create_new_session",
    "_create_branched_session",
)


def _load_module_ast() -> ast.Module:
    return ast.parse(_SRC_PATH.read_text(encoding="utf-8"), filename=str(_SRC_PATH))


def _get_class(tree: ast.Module, class_name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(f"未找到类 {class_name}")


def _get_method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"未找到方法 {cls.name}.{name}")


def _find_calls(method: ast.FunctionDef, name: str) -> list:
    """收集方法体内所有 ``<name>(...)`` 调用的 ast.Call 节点（按源码顺序）。

    兼容 self.xxx(...) 与模块级 xxx(...) 两种形式（ast.Attribute / ast.Name）。
    """
    calls = []
    for sub in ast.walk(method):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Attribute) and func.attr == name:
            calls.append(sub)
        elif isinstance(func, ast.Name) and func.id == name:
            calls.append(sub)
    return calls


# ═══════════════════════════════════════════════════════════════
# 1. AST 静态检查（防回归）
# ═══════════════════════════════════════════════════════════════


class TestRefreshHistoryPanelOnSavePaths:
    """三个保存路径末尾必须刷新历史面板"""

    @pytest.mark.parametrize("method_name", TARGET_METHODS)
    def test_must_call_refresh_history_card_if_visible(self, method_name: str):
        """方法体内必须出现 refresh_history_card_if_visible(...) 调用"""
        tree = _load_module_ast()
        cls = _get_class(tree, "OpenAIChatToolWindow")
        method = _get_method(cls, method_name)
        calls = _find_calls(method, "refresh_history_card_if_visible")
        assert calls, (
            f"{method_name} 末尾必须调用 refresh_history_card_if_visible(...)，"
            "否则历史面板已展开时保存会话后列表停留在保存前快照。"
        )

    @pytest.mark.parametrize("method_name", TARGET_METHODS)
    def test_call_arguments_use_self_history_card_and_refresh_func(self, method_name: str):
        """调用参数：第一个参数是 self._history_card，第二个是 self._refresh_history_toggle_panel"""
        tree = _load_module_ast()
        cls = _get_class(tree, "OpenAIChatToolWindow")
        method = _get_method(cls, method_name)
        calls = _find_calls(method, "refresh_history_card_if_visible")

        matched = []
        for call in calls:
            if len(call.args) < 2:
                continue
            a0, a1 = call.args[0], call.args[1]
            if (
                isinstance(a0, ast.Attribute)
                and a0.attr == "_history_card"
                and isinstance(a1, ast.Attribute)
                and a1.attr == "_refresh_history_toggle_panel"
            ):
                matched.append(call)
        assert matched, (
            f"{method_name} 必须存在 refresh_history_card_if_visible("
            "self._history_card, self._refresh_history_toggle_panel) 调用。"
        )


# ═══════════════════════════════════════════════════════════════
# 2. 行为测试（patch refresh_history_card_if_visible 验证调用）
# ═══════════════════════════════════════════════════════════════


def _make_min_stub():
    """构造最小窗口 stub，避免依赖真实 Qt 控件。

    仅为 _save_current_session_to_history / _auto_save_current_session
    跑通非 UI 分支所需字段。
    """
    from app.main_widget import OpenAIChatToolWindow

    inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)

    inst._session_dirty = True
    inst._current_session_id = None
    inst._current_project = "默认项目"

    session = MagicMock()
    session.messages = [{"role": "user", "content": "hi"}]
    session.session_id = "sess-001"
    session.system_prompt = ""
    session.compaction_state = {}
    session.compaction_cache = {}
    inst.session_manager = MagicMock()
    inst.session_manager.get_current_session.return_value = session

    history_manager = MagicMock()
    history_manager.save_session.return_value = None
    history_manager.update_session.return_value = None
    inst.history_manager = history_manager

    inst._team_run_id = None
    inst._team_name = ""
    inst._team_agent_name = ""

    inst._refresh_history_toggle_panel = MagicMock()
    inst._update_node_preview = MagicMock()
    inst._get_current_worktree_path = MagicMock(return_value="")
    inst._resolve_session_project_fallback = MagicMock(return_value="默认项目")

    return inst


class TestSaveCurrentSessionToHistoryBehavior:
    """行为验证：_save_current_session_to_history 触发刷新"""

    def _target_class(self):
        from app.main_widget import OpenAIChatToolWindow

        return OpenAIChatToolWindow

    def test_save_calls_refresh(self):
        OpenAIChatToolWindow = self._target_class()
        stub = _make_min_stub()
        stub._history_card = None

        with patch("app.main_widget.refresh_history_card_if_visible") as spy:
            OpenAIChatToolWindow._save_current_session_to_history(stub)
            spy.assert_called_once_with(stub._history_card, stub._refresh_history_toggle_panel)

    def test_save_still_persists(self):
        OpenAIChatToolWindow = self._target_class()
        stub = _make_min_stub()
        stub._history_card = None
        with patch("app.main_widget.refresh_history_card_if_visible"):
            OpenAIChatToolWindow._save_current_session_to_history(stub)
        stub.history_manager.save_session.assert_called_once()
        assert stub._session_dirty is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
