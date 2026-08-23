# -*- coding: utf-8 -*-
"""回归测试：欢迎卡片插件热重载链路 + 窗口契约属性缺失时的健壮性

修复背景（2026-08-23）：
插件安装/卸载触发 load_plugin → _schedule_welcome_refresh → _refresh_welcome_cards。
实证根因（loguru 日志 19:19:10）：
1. plugin override 异常 → _register_system_ui_modules/get_ui_module 半残 →
   窗口契约属性（_history_card 等）缺失
2. _notify_history_data_changed 访问 self._history_card 抛 AttributeError →
   _create_new_session 末尾中断 → 欢迎卡片重建链断
3. _refresh_welcome_cards 原 except: pass 完全静默，无法追踪

修复：
A. setup_ui 中 system_cards_module.build 失败回退 SystemCardsModule 默认 build
B. _notify_history_data_changed 用 getattr 防御 _history_card 缺失
C. _refresh_welcome_cards 按窗口 catch + log，DEBUG 暴露调度数

测试策略：纯 AST 守卫（防维护期被无意改回）+ 单一行为测试验证 _refresh_welcome_cards 健壮。
"""

import ast
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")


def _read_source(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _find_method(tree: ast.AST, cls_name: str, method_name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return item
    return None


# ─── 1. AST 守卫：修复点必须保留 ─────────────────────────────


class TestSourceGuards:
    """防止维护期被无意改回"""

    def test_refresh_welcome_cards_has_per_window_try(self):
        """_refresh_welcome_cards 必须按窗口 try/except（而非全函数 except: pass）"""
        src = _read_source("app/plugins/registries/ui_plugin_registry.py")
        tree = ast.parse(src)
        method = _find_method(tree, "UIPluginRegistry", "_refresh_welcome_cards")
        assert method is not None, "_refresh_welcome_cards 方法缺失"
        has_for_with_try = False
        for node in ast.walk(method):
            if isinstance(node, ast.For):
                for child in ast.walk(node):
                    if isinstance(child, ast.Try):
                        has_for_with_try = True
                        break
        assert has_for_with_try, "_refresh_welcome_cards 必须在循环内 try/except 按窗口处理"

    def test_refresh_welcome_cards_has_debug_log(self):
        """_refresh_welcome_cards 必须有 DEBUG 日志输出决策统计"""
        src = _read_source("app/plugins/registries/ui_plugin_registry.py")
        tree = ast.parse(src)
        method = _find_method(tree, "UIPluginRegistry", "_refresh_welcome_cards")
        assert method is not None
        method_src = ast.unparse(method)
        assert "logger.debug" in method_src, "缺少 logger.debug 调用"
        assert "total=" in method_src, "DEBUG 日志必须包含 total= 决策统计"
        assert "invalidated=" in method_src, "DEBUG 日志必须包含 invalidated= 决策统计"

    def test_notify_history_data_changed_uses_getattr_for_history_card(self):
        """_notify_history_data_changed 必须用 getattr 防御 _history_card 缺失"""
        src = _read_source("app/main_widget.py")
        tree = ast.parse(src)
        method = _find_method(tree, "OpenAIChatToolWindow", "_notify_history_data_changed")
        assert method is not None
        method_src = ast.unparse(method)
        # ast.unparse 默认输出单引号字符串
        assert "getattr(self, '_history_card'" in method_src or 'getattr(self, "_history_card"' in method_src, (
            "_notify_history_data_changed 必须用 getattr(self, '_history_card', ...) "
            "防御契约属性缺失"
        )

    def test_notify_history_data_changed_no_direct_self_history_card(self):
        """refresh_history_card_if_visible 不能直接传 self._history_card"""
        src = _read_source("app/main_widget.py")
        tree = ast.parse(src)
        method = _find_method(tree, "OpenAIChatToolWindow", "_notify_history_data_changed")
        assert method is not None
        method_src = ast.unparse(method)
        problematic = re.search(r"refresh_history_card_if_visible\(\s*self\._history_card", method_src)
        assert not problematic, (
            "refresh_history_card_if_visible 必须传 getattr 后的局部变量 history_card"
        )

    def test_setup_ui_system_cards_build_has_fallback(self):
        """setup_ui 中 system_cards_module.build 必须包 try/except + SystemCardsModule 兜底"""
        src = _read_source("app/main_widget.py")
        tree = ast.parse(src)
        method = _find_method(tree, "OpenAIChatToolWindow", "setup_ui")
        assert method is not None
        method_src = ast.unparse(method)
        assert "system_cards_module.build(self)" in method_src
        # 兜底必须直接实例化 SystemCardsModule
        assert "SystemCardsModule()" in method_src, (
            "setup_ui 必须有 SystemCardsModule() 兜底调用"
        )
        # 必须有两次 build(self) 调用（主路径 + 兜底）
        assert method_src.count("build(self)") >= 2, (
            "必须有主路径 + 兜底两次 build 调用"
        )


# ─── 2. 行为测试：_refresh_welcome_cards 健壮性 ──────────────


class TestRefreshWelcomeCardsRobustness:
    """_refresh_welcome_cards 在窗口方法抛异常时不应崩整个刷新流程

    不依赖 qapp fixture（offscreen + DriFox 完整导入链下会触发 qfluentwidgets
    等组件初始化，可能 STATUS_STACK_BUFFER_OVERRUN）。仅用 QApplication
    单例即可（被测试函数实际只读 list / 调用 mock）。
    """

    def setup_method(self):
        # 确保 QApplication 存在（loguru 在 sink 内引用 QApplication 不需要，
        # 但 import ui_plugin_registry 会触发 Qt 系统初始化）
        from PyQt5.QtWidgets import QApplication

        self._app = QApplication.instance() or QApplication([])

        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        self.reg = UIPluginRegistry()
        UIPluginRegistry._instance = self.reg

    def test_skips_window_without_invalidate_method(self):
        """窗口无 _invalidate_welcome_card 时跳过（兼容旧窗口）"""
        from types import SimpleNamespace

        mw = SimpleNamespace(_window_id="w1")
        self.reg._window_main_widgets = {"w1": mw}
        # 不抛
        self.reg._refresh_welcome_cards()

    def test_window_exception_does_not_abort_others(self):
        """某窗口 _invalidate_welcome_card 抛异常 → 其他窗口仍处理 + WARNING 暴露"""
        from unittest.mock import MagicMock

        from loguru import logger

        captured = []

        def sink(msg):
            captured.append(msg)

        sink_id = logger.add(sink, level="WARNING")
        try:
            mw_bad = MagicMock()
            mw_bad._window_id = "bad"
            mw_bad._invalidate_welcome_card.side_effect = RuntimeError("QWebEngineView init failed")
            mw_bad._welcome_card_cache = {"bad": MagicMock()}
            mw_bad._displayed_session_id = None
            mw_bad._schedule_initial_welcome = MagicMock()

            mw_ok = MagicMock()
            mw_ok._window_id = "ok"
            mw_ok._invalidate_welcome_card = MagicMock()
            mw_ok._welcome_card_cache = {"ok": MagicMock()}
            mw_ok._displayed_session_id = None
            mw_ok._schedule_initial_welcome = MagicMock()

            self.reg._window_main_widgets = {"bad": mw_bad, "ok": mw_ok}

            self.reg._refresh_welcome_cards()

            mw_bad._invalidate_welcome_card.assert_called_once()
            mw_ok._invalidate_welcome_card.assert_called_once()
            mw_ok._schedule_initial_welcome.assert_called_once()

            joined = "\n".join(str(m) for m in captured)
            assert "处理失败" in joined and "bad" in joined, (
                f"异常窗口的失败必须 WARNING 暴露，captured={captured!r}"
            )
        finally:
            logger.remove(sink_id)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
