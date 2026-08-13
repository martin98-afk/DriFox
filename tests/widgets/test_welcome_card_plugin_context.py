# -*- coding: utf-8 -*-
"""回归测试：欢迎卡片插件 tab 多窗口隔离 + 批量刷新合并

修复背景（2026-08-13）：
1. 【多标签页内容串项目】_render_welcome_body 调用插件 render_func 时只传
   {"is_dark"}，插件只能回读全局状态（Settings.current_project / 全局 workdir）
   取项目信息。多窗口场景下 A 窗口的欢迎卡片会被渲染成 B 窗口的项目内容
   （切标签页后全局配置指向最新窗口的项目）。
   → render_func 注入窗口上下文（project_name / project_root / window_id）。
2. 【插件多 → 新建会话/启动卡顿】插件批量加载时逐个触发 _refresh_welcome_cards，
   每个注册 welcome tab 的插件都同步重建一次 QWebEngineView（100-500ms/次）。
   → debounce 合并为单次刷新 + 交错时间片调度（_schedule_initial_welcome）。

测试策略：
- _render_welcome_body 是模块级纯函数，直接注入 stub 插件 tab 验证上下文注入。
- UIPluginRegistry 刷新路径用 MagicMock 窗口验证调度行为。

注意：**不缓存 render_func 结果**——异步采集型插件（如 project-dashboard）首次
渲染返回「加载中」占位，采集完成后重渲染拿真实图表；缓存占位会阻塞该机制。
"""

import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.core.ui_plugin_registry import UIPluginRegistry
from app.widgets import message_card as mc


# ─── helpers ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _cleanup_registry():
    """每个测试前清空注册表，避免测试间相互污染"""
    UIPluginRegistry.get_instance().reset()
    yield
    UIPluginRegistry.get_instance().reset()


def _register_stub_tab(mode_key: str = "proj-stats", seen: list = None) -> None:
    """注册一个记录调用上下文的 stub 插件 tab"""
    reg = UIPluginRegistry.get_instance()

    def render_func(ctx):
        if seen is not None:
            seen.append(dict(ctx))
        return f"<div>插件 {ctx.get('project_name', '')}</div>"

    reg.register_welcome_tab(
        plugin_name="test-plugin",
        mode_key=mode_key,
        label="项目统计",
        render_func=render_func,
    )


# ─── 1. 窗口上下文注入（多标签页内容串项目修复）───────────────


class TestWindowContextInjection:
    """render_func 必须收到窗口级 project 上下文（而非全局状态）"""

    def test_render_func_receives_window_context(self):
        seen = []
        _register_stub_tab(seen=seen)
        mc._render_welcome_body(
            "proj-stats",
            [],
            [],
            window_context={"project_name": "项目X", "project_root": "D:/x", "window_id": "w1"},
        )
        assert seen, "render_func 应被调用"
        assert seen[-1]["project_name"] == "项目X"
        assert seen[-1]["project_root"] == "D:/x"
        assert seen[-1]["window_id"] == "w1"

    def test_two_windows_different_projects_isolated(self):
        """窗口 A（项目X）与窗口 B（项目Y）渲染互不串数据"""
        seen = []
        _register_stub_tab(seen=seen)

        mc._render_welcome_body(
            "proj-stats",
            [],
            [],
            window_context={"project_name": "项目X", "project_root": "D:/x", "window_id": "wA"},
        )
        mc._render_welcome_body(
            "proj-stats",
            [],
            [],
            window_context={"project_name": "项目Y", "project_root": "D:/y", "window_id": "wB"},
        )

        assert [c["project_name"] for c in seen] == ["项目X", "项目Y"]
        assert [c["window_id"] for c in seen] == ["wA", "wB"]

    def test_no_window_context_falls_back_to_is_dark_only(self):
        """无 window_context 时（旧调用方）仍只传 is_dark，向后兼容"""
        seen = []
        _register_stub_tab(seen=seen)
        mc._render_welcome_body("proj-stats", [], [], window_context=None)
        ctx = seen[-1]
        assert "is_dark" in ctx
        assert ctx.get("project_name", "") == ""

    def test_render_func_called_every_time_no_cache(self):
        """异步采集型插件（project-dashboard）：每次渲染都重新调用 render_func

        插件首次渲染返回「加载中」占位，后台采集完成后再次调用 render_func
        返回真实图表。若主程序缓存了占位 HTML，重渲染被短路 → 数据永远不显示。
        """
        seen = []
        _register_stub_tab(seen=seen)
        ctx = {"project_name": "项目X", "project_root": "D:/x"}

        mc._render_welcome_body("proj-stats", [], [], window_context=ctx)  # 占位
        mc._render_welcome_body("proj-stats", [], [], window_context=ctx)  # 采集完成 → 图表

        assert len(seen) == 2, "render_func 必须每次重渲染都执行（不得缓存占位内容）"


# ─── 2. create_welcome_card 透传 context_provider ────────────


class TestCreateWelcomeCardContextProvider:
    """create_welcome_card 必须把窗口 context provider 交给卡片"""

    def test_signature_accepts_context_provider(self):
        """AST/签名检查：create_welcome_card 支持 context_provider 参数"""
        src = Path(__file__).resolve().parent.parent.parent / "app" / "widgets" / "message_card.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "create_welcome_card":
                arg_names = [a.arg for a in node.args.args]
                assert "context_provider" in arg_names, "create_welcome_card 缺少 context_provider 参数"
                return
        raise AssertionError("未找到 create_welcome_card 函数")

    def test_card_stores_and_uses_provider(self, _qt_app):
        """卡片存储 provider，渲染时调用并拿到窗口上下文"""
        _register_stub_tab(seen=None)
        provider = MagicMock(
            return_value={
                "project_name": "项目X",
                "project_root": "D:/x",
                "window_id": "wA",
                "is_dark": False,
            }
        )
        card = mc.create_welcome_card(
            recent_sessions=[],
            top_by_count=[],
            mode="proj-stats",
            context_provider=provider,
        )
        assert card._welcome_ctx_provider is provider
        # set_welcome_content 渲染时应调用 provider 并写入 pending md
        assert "项目X" in card._pending_welcome_md
        provider.assert_called_once()
        card.deleteLater()

    def test_main_widget_passes_build_ui_context(self):
        """main_widget 调用点必须传 context_provider=self._build_ui_context（AST）"""
        src = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "create_welcome_card":
                for kw in node.keywords:
                    if kw.arg == "context_provider":
                        assert "build_ui_context" in ast.unparse(kw.value)
                        found = True
        assert found, "main_widget 调用 create_welcome_card 未传 context_provider"


# ─── 3. UIPluginRegistry 批量刷新合并（插件多 → 卡顿修复）──────


class TestWelcomeRefreshDebounce:
    """插件批量加载/卸载时欢迎卡片刷新合并为单次"""

    def _make_registry(self):
        reg = UIPluginRegistry.__new__(UIPluginRegistry)
        reg._welcome_refresh_pending = False
        return reg

    def test_schedule_merges_multiple_calls(self, _qt_app):
        """同一事件批次多次 schedule 只触发一次 flush"""
        reg = self._make_registry()
        reg._window_main_widgets = {}  # 显式隔离：不受其他测试残留窗口影响
        reg._flush_welcome_refresh = MagicMock()

        reg._schedule_welcome_refresh()
        reg._schedule_welcome_refresh()
        reg._schedule_welcome_refresh()

        # flush 尚未执行（singleShot(0) 排队中，事件循环未处理）
        assert reg._welcome_refresh_pending is True
        reg._flush_welcome_refresh.assert_not_called()

        # 处理事件循环后恰好执行一次
        _qt_app.processEvents()
        reg._flush_welcome_refresh.assert_called_once()

    def test_flush_resets_pending(self):
        """flush 执行后 pending 标志复位，允许下一次调度"""
        reg = self._make_registry()
        reg._refresh_welcome_cards = MagicMock()
        reg._flush_welcome_refresh()
        assert reg._welcome_refresh_pending is False

    def test_refresh_uses_staggered_scheduling(self):
        """_refresh_welcome_cards 必须走 _schedule_initial_welcome（交错调度）"""
        reg = UIPluginRegistry.get_instance()
        reg.reset()  # 隔离：清空其他测试残留的窗口引用
        mw = MagicMock()
        mw._window_id = "w1"
        mw._invalidate_welcome_card = MagicMock()
        mw._welcome_card_cache = {"w1": MagicMock()}
        mw._displayed_session_id = None
        mw._schedule_initial_welcome = MagicMock()
        reg._window_main_widgets = {"w1": mw}

        reg._refresh_welcome_cards()

        mw._invalidate_welcome_card.assert_called_once()
        mw._schedule_initial_welcome.assert_called_once()
        mw._show_initial_welcome.assert_not_called()

    def test_refresh_fallback_to_sync_when_no_stagger(self):
        """窗口无 _schedule_initial_welcome 时回退同步 _show_initial_welcome"""
        from types import SimpleNamespace

        reg = UIPluginRegistry.get_instance()
        reg.reset()  # 隔离：清空其他测试残留的窗口引用
        mw = SimpleNamespace(
            _window_id="w1",
            _invalidate_welcome_card=MagicMock(),
            _welcome_card_cache={"w1": MagicMock()},
            _displayed_session_id=None,
            _show_initial_welcome=MagicMock(),
        )
        reg._window_main_widgets = {"w1": mw}

        reg._refresh_welcome_cards()

        mw._show_initial_welcome.assert_called_once()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
