# -*- coding: utf-8 -*-
"""回归测试：欢迎卡片缓存失效

修复背景（2026-08-01）：
- _welcome_card_cache 仅按 _window_id 缓存，但卡片内容依赖
  _current_project / _current_agent。
- 切换项目/智能体/新建会话时必须显式失效，否则会展示上一个项目/智能体的陈旧数据。
- 之前唯一的失效路径 sip.isdeleted(cached) 存在竞态：
  _clear_chat_area 之后 QTimer.singleShot(0, _show_initial_welcome) 可能
  在 deleteLater 实际执行前先触发，导致缓存命中返回旧卡片。

测试策略：
- AST 静态检查：保证关键方法存在、签名正确，且在变更点已调用
  _invalidate_welcome_card()。
- 功能单元测试：模拟 _welcome_card_cache 实际状态，验证失效行为。
"""

import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ─── AST helpers ────────────────────────────────────────────


def _get_main_widget_src() -> str:
    repo_root = Path(__file__).resolve().parent.parent.parent
    return (repo_root / "app" / "main_widget.py").read_text(encoding="utf-8")


def _get_target_class() -> ast.ClassDef:
    src = _get_main_widget_src()
    tree = ast.parse(src, filename="main_widget.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "OpenAIChatToolWindow":
            return node
    raise AssertionError("未找到 OpenAIChatToolWindow 类")


def _get_method(cls: ast.ClassDef, name: str) -> ast.FunctionDef | None:
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _method_calls(method: ast.FunctionDef, attr_chain: str) -> bool:
    """检查方法体中是否调用 self.attr_chain(...)。"""
    target_attr = attr_chain.split(".")[-1]
    for node in ast.walk(method):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == target_attr:
                if _expr_starts_with(func, "self"):
                    return True
    return False


def _expr_starts_with(node: ast.AST, prefix: str) -> bool:
    """检查 ast 表达式是否以 prefix 开头（self.x.y.z）。"""
    if isinstance(node, ast.Name):
        return node.id == prefix
    if isinstance(node, ast.Attribute):
        return _expr_starts_with(node.value, prefix)
    return False


# ─── 1. AST 静态检查 ─────────────────────────────────────────


class TestInvalidateWelcomeCardMethod:
    """_invalidate_welcome_card 方法存在 + 签名正确"""

    def test_method_exists(self):
        cls = _get_target_class()
        method = _get_method(cls, "_invalidate_welcome_card")
        assert method is not None, "缺少 _invalidate_welcome_card 方法"

    def test_method_signature(self):
        """_invalidate_welcome_card(self) — 仅 self 参数"""
        cls = _get_target_class()
        method = _get_method(cls, "_invalidate_welcome_card")
        assert method is not None
        args = method.args.args
        assert len(args) == 1, f"_invalidate_welcome_card 参数错误: {[a.arg for a in args]}"
        assert args[0].arg == "self"

    def test_method_uses_sip_isdeleted(self):
        """方法内应检查 sip.isdeleted（防御性双保险）"""
        cls = _get_target_class()
        method = _get_method(cls, "_invalidate_welcome_card")
        assert method is not None
        src = ast.unparse(method)
        assert "isdeleted" in src, "失效方法应检查 sip.isdeleted"


class TestInvalidationCallSites:
    """关键变更点已调用 _invalidate_welcome_card()"""

    def test_create_new_session_invalidates(self):
        """_create_new_session 入口处失效（避免 sip.isdeleted 竞态）"""
        cls = _get_target_class()
        method = _get_method(cls, "_create_new_session")
        assert method is not None
        assert _method_calls(method, "_invalidate_welcome_card"), (
            "_create_new_session 未调用 _invalidate_welcome_card()"
        )

    def test_on_project_selected_invalidates(self):
        """_on_project_selected 切换项目后失效"""
        cls = _get_target_class()
        method = _get_method(cls, "_on_project_selected")
        assert method is not None
        assert _method_calls(method, "_invalidate_welcome_card"), (
            "_on_project_selected 未调用 _invalidate_welcome_card()"
        )

    def test_on_agent_changed_invalidates(self):
        """_on_agent_changed 切换智能体后失效"""
        cls = _get_target_class()
        method = _get_method(cls, "_on_agent_changed")
        assert method is not None
        assert _method_calls(method, "_invalidate_welcome_card"), "_on_agent_changed 未调用 _invalidate_welcome_card()"

    def test_on_archived_session_deleted_invalidates(self):
        """_on_archived_session_deleted 归档删除后失效"""
        cls = _get_target_class()
        method = _get_method(cls, "_on_archived_session_deleted")
        assert method is not None
        assert _method_calls(method, "_invalidate_welcome_card"), (
            "_on_archived_session_deleted 未调用 _invalidate_welcome_card()"
        )

    def test_on_archived_session_renamed_invalidates(self):
        """_on_archived_session_renamed 归档重命名后失效"""
        cls = _get_target_class()
        method = _get_method(cls, "_on_archived_session_renamed")
        assert method is not None
        assert _method_calls(method, "_invalidate_welcome_card"), (
            "_on_archived_session_renamed 未调用 _invalidate_welcome_card()"
        )


# ─── 2. 功能单元测试 ─────────────────────────────────────────


class TestInvalidationBehavior:
    """直接验证失效行为：pop 缓存 + 摘除 parent + deleteLater"""

    def _make_widget(self):
        """构造一个最小化的 widget 用于测试失效逻辑。

        由于 OpenAIChatToolWindow 依赖极重，直接实例化会失败。
        这里走一个轻量替身：跳过 __init__，仅设置必要属性，复用真实方法。
        """
        from app.main_widget import OpenAIChatToolWindow

        widget = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
        widget._window_id = "win_test_01"
        widget._welcome_card_cache = {}
        widget._displayed_session_id = None
        widget.chat_layout = MagicMock()
        return widget

    def test_no_op_when_cache_empty(self):
        """缓存为空时调用不应抛异常"""
        widget = self._make_widget()
        assert widget._welcome_card_cache == {}
        widget._invalidate_welcome_card()
        assert widget._welcome_card_cache == {}

    def test_pops_cache_entry(self):
        """缓存有值时必须被 pop 出去"""
        widget = self._make_widget()
        cached = MagicMock()
        cached.parent.return_value = None
        widget._welcome_card_cache[widget._window_id] = cached

        widget._invalidate_welcome_card()

        assert widget._window_id not in widget._welcome_card_cache
        assert cached.deleteLater.called, "应调用 cached.deleteLater()"

    def test_handles_sip_deleted_widget(self, _qt_app):
        """sip.isdeleted 返回 True 时不应再操作 widget"""
        from PyQt5.QtCore import QObject

        widget = self._make_widget()
        cached = QObject()
        widget._welcome_card_cache[widget._window_id] = cached
        # 主动释放
        cached.deleteLater()
        # 立即调用：sip.isdeleted 可能为 False（异步），也可能为 True
        # 不管哪种情况，都不应抛异常
        widget._invalidate_welcome_card()
        # 缓存必然被 pop
        assert widget._window_id not in widget._welcome_card_cache


# ─── 3. workdir 同步后看板重渲染（project-dashboard 修复回归） ─────


class TestRerenderWelcomeCard:
    """_rerender_welcome_card：workdir 延迟同步完成后强制重渲染看板 tab

    修复背景（2026-08-13）：project-dashboard 看板依赖 project_root，
    软件启动/项目切换瞬间 workdir 尚未同步到 tool_executor，UI provider
    返回空 project_root（此前是 os.getcwd() 兜底 → 误显示软件启动目录的
    git 信息）。_sync_working_directory 延迟同步完成后必须强制重渲染
    欢迎卡片，否则看板停留在"未检测到 git 项目"。
    """

    def _make_widget(self):
        from app.main_widget import OpenAIChatToolWindow

        widget = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
        widget._window_id = "win_rerender_01"
        widget._welcome_card_cache = {}
        return widget

    def test_method_exists(self):
        """_rerender_welcome_card 方法存在（AST）"""
        cls = _get_target_class()
        assert _get_method(cls, "_rerender_welcome_card") is not None

    def test_sync_workdir_calls_rerender_when_changed(self):
        """_sync_working_directory 在 workdir 变化时应调用 _rerender_welcome_card（AST）"""
        cls = _get_target_class()
        method = _get_method(cls, "_sync_working_directory")
        assert method is not None
        assert _method_calls(method, "_rerender_welcome_card"), (
            "_sync_working_directory 未在 workdir 变化时调用 _rerender_welcome_card()"
        )

    def test_no_op_when_cache_empty(self):
        """缓存为空时调用不应抛异常"""
        widget = self._make_widget()
        widget._rerender_welcome_card()  # 不抛异常即通过

    def test_rerenders_same_mode(self):
        """缓存有卡片时以同 mode 重渲染（set_welcome_mode 同 mode 也会刷新 body）"""
        widget = self._make_widget()
        card = MagicMock()
        card._welcome_mode = "project-dashboard"
        widget._welcome_card_cache[widget._window_id] = card

        widget._rerender_welcome_card()

        card.set_welcome_mode.assert_called_once_with("project-dashboard")

    def test_skips_when_no_mode(self):
        """卡片无 mode 时跳过，不调用 set_welcome_mode"""
        widget = self._make_widget()
        card = MagicMock()
        card._welcome_mode = ""
        widget._welcome_card_cache[widget._window_id] = card

        widget._rerender_welcome_card()

        card.set_welcome_mode.assert_not_called()

    def test_different_window_id_not_touched(self):
        """只 pop 自己 _window_id 对应的项，不影响其他窗口"""
        widget = self._make_widget()
        other_widget_cache = MagicMock()
        widget._welcome_card_cache["win_other"] = other_widget_cache
        own_cache = MagicMock()
        own_cache.parent.return_value = None
        widget._welcome_card_cache[widget._window_id] = own_cache

        widget._invalidate_welcome_card()

        assert widget._window_id not in widget._welcome_card_cache
        assert widget._welcome_card_cache["win_other"] is other_widget_cache
        assert not other_widget_cache.deleteLater.called

    def test_skips_builtin_modes(self):
        """内置 mode（sessions/changelog）跳过重渲染：不依赖 project_root，
        重渲染只会因随机 greeting 使 HTML 变化、触发进入动画重复播放"""
        for builtin_mode in ("sessions", "changelog"):
            widget = self._make_widget()
            card = MagicMock()
            card._welcome_mode = builtin_mode
            widget._welcome_card_cache[widget._window_id] = card

            widget._rerender_welcome_card()

            card.set_welcome_mode.assert_not_called(), f"{builtin_mode} 不应触发重渲染"


# ─── 3. 回归 bug 场景模拟 ─────────────────────────────────────


class TestRegressionScenarios:
    """模拟用户场景：点击"新建"按钮 / 切换项目 / 切换智能体"""

    def test_new_session_button_invalidates_before_race(self):
        """模拟竞态：_create_new_session 必须在 _clear_chat_area 之前先失效

        修复前流程：
          _create_new_session -> _clear_chat_area (deleteLater 异步)
                              -> QTimer.singleShot(0, _show_initial_welcome)
          → 若 singleShot 先于 deleteLater 执行，sip.isdeleted=False，缓存命中旧卡片

        修复后流程：
          _create_new_session -> _invalidate_welcome_card (同步，pop+delete)
                              -> ... _clear_chat_area ...
                              -> QTimer.singleShot(0, _show_initial_welcome)
          → 缓存必 miss，下次 _get_or_create_welcome_card 重建
        """
        cls = _get_target_class()
        method = _get_method(cls, "_create_new_session")
        assert method is not None
        src = ast.unparse(method)

        invalidate_pos = src.find("_invalidate_welcome_card")
        clear_pos = src.find("_clear_chat_area")

        assert invalidate_pos != -1, "_create_new_session 未调用 _invalidate_welcome_card"
        assert clear_pos != -1, "_create_new_session 未调用 _clear_chat_area"
        # 失效必须在清空之前（消除 sip.isdeleted 竞态的关键）
        assert invalidate_pos < clear_pos, (
            "失效调用必须在 _clear_chat_area 之前，否则 QTimer.singleShot 仍可能在 deleteLater 实际执行前触发"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
