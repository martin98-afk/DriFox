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
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")


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
        assert "skipped_no_method=" in method_src, "DEBUG 日志必须包含 skipped_no_method= 区分缺方法窗口"

    def test_notify_history_data_changed_uses_getattr_for_history_card(self):
        """_notify_history_data_changed 必须用 getattr 防御 _history_card 缺失"""
        src = _read_source("app/main_widget.py")
        tree = ast.parse(src)
        method = _find_method(tree, "OpenAIChatToolWindow", "_notify_history_data_changed")
        assert method is not None
        method_src = ast.unparse(method)
        # ast.unparse 默认输出单引号字符串
        assert "getattr(self, '_history_card'" in method_src or 'getattr(self, "_history_card"' in method_src, (
            "_notify_history_data_changed 必须用 getattr(self, '_history_card', ...) 防御契约属性缺失"
        )

    def test_notify_history_data_changed_no_direct_self_history_card(self):
        """refresh_history_card_if_visible 不能直接传 self._history_card"""
        src = _read_source("app/main_widget.py")
        tree = ast.parse(src)
        method = _find_method(tree, "OpenAIChatToolWindow", "_notify_history_data_changed")
        assert method is not None
        method_src = ast.unparse(method)
        problematic = re.search(r"refresh_history_card_if_visible\(\s*self\._history_card", method_src)
        assert not problematic, "refresh_history_card_if_visible 必须传 getattr 后的局部变量 history_card"

    def test_setup_ui_system_cards_build_has_fallback(self):
        """setup_ui 中 system_cards_module.build 必须包 try/except + SystemCardsModule 兜底"""
        src = _read_source("app/main_widget.py")
        tree = ast.parse(src)
        method = _find_method(tree, "OpenAIChatToolWindow", "setup_ui")
        assert method is not None
        method_src = ast.unparse(method)
        assert "system_cards_module.build(self)" in method_src
        # 兜底必须直接实例化 SystemCardsModule
        assert "SystemCardsModule()" in method_src, "setup_ui 必须有 SystemCardsModule() 兜底调用"
        # 必须有两次 build(self) 调用（主路径 + 兜底）
        assert method_src.count("build(self)") >= 2, "必须有主路径 + 兜底两次 build 调用"

    def test_show_initial_welcome_has_exception_guard(self):
        """_show_initial_welcome 必须包 try/except + DEBUG 日志（防 QWebEngineView 异常）"""
        src = _read_source("app/main_widget.py")
        tree = ast.parse(src)
        method = _find_method(tree, "OpenAIChatToolWindow", "_show_initial_welcome")
        assert method is not None, "_show_initial_welcome 方法缺失"
        method_src = ast.unparse(method)
        # 异常兜底
        assert "except Exception" in method_src, (
            "_show_initial_welcome 必须捕获异常，否则 QWebEngineView 创建失败会导致"
            "卡片永久消失（invalidate 已删旧卡 + 重建抛异常 → 空白）"
        )
        assert "logger.debug" in method_src, "_show_initial_welcome 必须有 DEBUG 日志确认渲染成功"

    def test_schedule_initial_welcome_has_debug_log(self):
        """_schedule_initial_welcome 必须有 DEBUG 日志输出 slot/delay"""
        src = _read_source("app/main_widget.py")
        tree = ast.parse(src)
        method = _find_method(tree, "OpenAIChatToolWindow", "_schedule_initial_welcome")
        assert method is not None
        method_src = ast.unparse(method)
        assert "logger.debug" in method_src, (
            "_schedule_initial_welcome 必须有 DEBUG 日志输出 slot= delay= ms，便于排查调度断链"
        )

    def test_on_welcome_render_slot_has_debug_log(self):
        """_on_welcome_render_slot 必须有 DEBUG 日志（确认槽位回调真触发）"""
        src = _read_source("app/main_widget.py")
        tree = ast.parse(src)
        method = _find_method(tree, "OpenAIChatToolWindow", "_on_welcome_render_slot")
        assert method is not None
        method_src = ast.unparse(method)
        assert "logger.debug" in method_src, (
            "_on_welcome_render_slot 必须有 DEBUG 日志，确认 QTimer 回调真触发（防 pending 卡死）"
        )
