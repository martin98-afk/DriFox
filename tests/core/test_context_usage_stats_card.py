# -*- coding: utf-8 -*-
"""ContextUsageStatsCard 单元测试

测试范围：
1. _make_chart_colors_from_context() — 主题色映射逻辑（纯函数）
2. _format_number() — 数字格式化（纯函数）
3. _fast_estimate_tokens() — Token 估算（纯函数）
4. ContextUsageStatsCard.set_context_provider + 注册表集成（纯逻辑，不创建 Qt 控件）

设计说明：
- Qt 控件需要运行中 QApplication，在测试环境中不可用
- 因此集中测试纯函数逻辑和注册表集成
- 控件级测试（set_colors、show_card）建议在集成测试或手动验证中覆盖
"""
import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable, Dict

import pytest
from PyQt5.QtGui import QColor

# context-usage-stats 插件重构（T8）：纯函数已从 ui/cards.py 迁出——
# _fast_estimate_tokens/_estimate_messages_tokens → ui/data.py；
# _format_number/_make_chart_colors_from_context → ui/charts.py。
# 用唯一模块名加载，避免与其他插件的 ui 包冲突。
_SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "context-usage-stats" / "ui"

_cus_data_spec = importlib.util.spec_from_file_location("cus_data", _SCRIPT_DIR / "data.py")
_cus_data = importlib.util.module_from_spec(_cus_data_spec)
_cus_data_spec.loader.exec_module(_cus_data)
_fast_estimate_tokens = _cus_data._fast_estimate_tokens
_estimate_messages_tokens = _cus_data._estimate_messages_tokens

_cus_charts_spec = importlib.util.spec_from_file_location("cus_charts", _SCRIPT_DIR / "charts.py")
_cus_charts = importlib.util.module_from_spec(_cus_charts_spec)
_cus_charts_spec.loader.exec_module(_cus_charts)
_format_number = _cus_charts._format_number
_make_chart_colors_from_context = _cus_charts._make_chart_colors_from_context


# ══════════════════════════════════════════════════════════
# _make_chart_colors_from_context — 主题色映射
# ══════════════════════════════════════════════════════════


class TestMakeChartColorsFromContext:
    """测试上下文 → 图表配色映射"""

    def test_returns_dict_with_all_keys(self):
        """返回的字典包含所有图表需要的色键"""
        ctx = {"colors": {}, "is_dark": True, "font_family": "Segoe UI", "font_size": 14}
        result = _make_chart_colors_from_context(ctx)
        expected_keys = {
            "bar_fill", "bar_border", "line", "line_fill", "point",
            "grid", "text", "text_secondary", "card_bg",
            "accent", "warning", "success",
            "font_family", "font_size",
        }
        assert expected_keys.issubset(result.keys())

    def test_values_are_qcolor_for_color_keys(self):
        """色值键对应的值都是 QColor 实例"""
        ctx = {"colors": {}, "is_dark": True}
        result = _make_chart_colors_from_context(ctx)
        color_keys = {
            "bar_fill", "bar_border", "line", "line_fill", "point",
            "grid", "text", "text_secondary", "card_bg",
            "accent", "warning", "success",
        }
        for k in color_keys:
            assert isinstance(result[k], QColor), f"{k} should be QColor"

    def test_uses_provided_theme_colors(self):
        """当 context 提供了 colors 时，使用提供的色值"""
        ctx = {
            "colors": {
                "accent": "#ff0000",
                "success": "#00ff00",
                "text_primary": "#ffffff",
            },
            "is_dark": True,
        }
        result = _make_chart_colors_from_context(ctx)
        assert result["accent"].name() == "#ff0000"
        assert result["bar_border"].red() == 255  # bar_border 复用 accent

    def test_fallback_when_colors_empty(self):
        """当 context 没有 colors 时，使用 is_dark 决定的 fallback"""
        ctx = {"colors": {}, "is_dark": True}
        result_dark = _make_chart_colors_from_context(ctx)
        ctx_light = {"colors": {}, "is_dark": False}
        result_light = _make_chart_colors_from_context(ctx_light)
        # 深色和浅色的 fallback accent 不同
        assert result_dark["accent"].name() != result_light["accent"].name()

    def test_passes_font_info(self):
        """上下文中的字体信息被透传"""
        ctx = {"colors": {}, "is_dark": True, "font_family": "Noto Sans", "font_size": 16}
        result = _make_chart_colors_from_context(ctx)
        assert result["font_family"] == "Noto Sans"
        assert result["font_size"] == 16

    def test_default_font_family_when_missing(self):
        """当 context 没有 font 信息时，使用默认字体"""
        ctx = {"colors": {}, "is_dark": True}
        result = _make_chart_colors_from_context(ctx)
        assert result["font_family"] == "Microsoft YaHei"
        assert result["font_size"] == 14

    def test_empty_colors_with_light_theme(self):
        """浅色模式空 colors 的 fallback"""
        ctx = {"colors": {}, "is_dark": False}
        result = _make_chart_colors_from_context(ctx)
        assert isinstance(result["accent"], QColor)
        # 浅色模式 accent 应为蓝色调（非深色模式的 98,160,234）
        r, g, b, _ = result["accent"].getRgb()
        assert (r, g, b) != (98, 160, 234)


# ══════════════════════════════════════════════════════════
# _format_number — 数字格式化
# ══════════════════════════════════════════════════════════


class TestFormatNumber:
    def test_under_1000(self):
        assert _format_number(999) == "999"

    def test_thousands(self):
        assert _format_number(1500) == "1.5k"

    def test_millions(self):
        assert _format_number(2500000) == "2.5M"

    def test_zero(self):
        assert _format_number(0) == "0"

    def test_exact_1000(self):
        assert _format_number(1000) == "1.0k"

    def test_large_number(self):
        assert _format_number(10_000_000) == "10.0M"


# ══════════════════════════════════════════════════════════
# _fast_estimate_tokens — Token 估算
# ══════════════════════════════════════════════════════════


class TestFastEstimateTokens:
    def test_empty_string(self):
        assert _fast_estimate_tokens("") == 0

    def test_none(self):
        assert _fast_estimate_tokens(None) == 0

    def test_english_only(self):
        result = _fast_estimate_tokens("Hello world")
        assert result >= 1
        # 11 chars / 4 ≈ 2.75 → max(1, 2) = 2
        assert result == 2

    def test_chinese_only(self):
        result = _fast_estimate_tokens("你好世界测试")
        assert result >= 1
        # 6 汉字 × 1.2 token/字 ≈ 7（data.py 现实现：int(chinese * 1.2 + non_chinese / 4.0)）
        assert result == 7

    def test_mixed_text(self):
        result = _fast_estimate_tokens("Hello 你好 World 世界")
        assert result >= 3


class TestEstimateMessagesTokens:
    def test_empty_string(self):
        assert _estimate_messages_tokens("") == 0

    def test_short_json(self):
        assert _estimate_messages_tokens("{}") == 0

    def test_short_str_under_10_chars(self):
        assert _estimate_messages_tokens("short") == 0

    def test_normal_json(self):
        json_str = '{"role": "user", "content": "Hello world test message here"}'
        result = _estimate_messages_tokens(json_str)
        assert result >= 1

    def test_long_json_string(self):
        result = _estimate_messages_tokens('{"role": "user", "content": "hello test msg world"}')
        assert result >= 1


# ══════════════════════════════════════════════════════════
# ContextUsageStatsCard 注册集成（纯逻辑测试，不创建 Qt 控件）
# ══════════════════════════════════════════════════════════


class TestContextUsageStatsCardRegistry:
    """通过 UIPluginRegistry 测试卡片注册流程（无需 Qt 控件实例化）"""

    def test_plugin_registers_floating_card(self):
        """register_ui 正确注册浮动卡片到 registry"""
        from app.core.ui_plugin_registry import UIPluginRegistry

        registry = UIPluginRegistry.get_instance()
        registry.reset()

        # 模拟注册
        registry.register_floating_card(
            plugin_name="context-usage-stats",
            card_id="context-usage-stats",
            widget_class=dict,  # 测试时用 dict 代替真实 widget_class
            container="bottom",
            title="上下文用量统计",
            default_visible=False,
        )
        cards = registry.get_floating_cards()
        assert "context-usage-stats" in cards
        info = cards["context-usage-stats"]
        assert info.plugin_name == "context-usage-stats"
        assert info.container == "bottom"
        assert info.title == "上下文用量统计"
        assert info.default_visible is False

        registry.reset()

    def test_plugin_unload_cleans_card(self):
        """卸载插件时浮动卡片注册被清理"""
        from app.core.ui_plugin_registry import UIPluginRegistry

        registry = UIPluginRegistry.get_instance()
        registry.reset()

        registry.register_floating_card(
            plugin_name="context-usage-stats",
            card_id="context-usage-stats",
            widget_class=dict,
            container="bottom",
            title="上下文用量统计",
        )
        assert "context-usage-stats" in registry.get_floating_cards()

        # 模拟已加载状态（unload_plugin 检查 _loaded_plugins）
        registry._loaded_plugins.add("context-usage-stats")
        registry.unload_plugin("context-usage-stats")
        assert "context-usage-stats" not in registry.get_floating_cards()

        registry.reset()

    def test_card_context_provider_contract(self):
        """验证 context provider 返回值的契约（字段完整性）"""
        from app.core.ui_plugin_registry import UIPluginRegistry

        registry = UIPluginRegistry.get_instance()
        registry.reset()

        # 模拟 set_context_provider 在 main_widget 中的行为
        def fake_context_provider():
            return {
                "project_root": "/test/project",
                "project_name": "test-project",
                "session_id": "session-123",
                "window_id": "win-1",
                "theme_id": "midnight",
                "theme_name": "深海蓝黑",
                "is_dark": True,
                "font_family": "Microsoft YaHei",
                "font_size": 14,
                "colors": {
                    "accent": "#66c6ff",
                    "text_primary": "#f3f6fc",
                    "card_bg": "rgba(22, 30, 45, 230)",
                },
            }

        registry.set_context_provider(fake_context_provider)
        ctx = fake_context_provider()

        # 检验契约字段
        assert "project_root" in ctx
        assert "project_name" in ctx
        assert "session_id" in ctx
        assert "window_id" in ctx
        assert "theme_id" in ctx
        assert "theme_name" in ctx
        assert "is_dark" in ctx
        assert "font_family" in ctx
        assert "font_size" in ctx
        assert "colors" in ctx
        assert "accent" in ctx["colors"]

        registry.reset()

    def test_context_provider_to_chart_colors(self):
        """完整链路：context provider → _make_chart_colors_from_context"""
        from app.core.ui_plugin_registry import UIPluginRegistry

        registry = UIPluginRegistry.get_instance()
        registry.reset()

        def fake_context_provider():
            return {
                "project_root": "/test",
                "project_name": "test",
                "session_id": "s1",
                "window_id": "w1",
                "theme_id": "amber",
                "theme_name": "琥珀",
                "is_dark": False,
                "font_family": "Arial",
                "font_size": 15,
                "colors": {
                    "accent": "#ff6600",
                    "success": "#22c55e",
                    "text_primary": "#1a1a1a",
                },
            }

        registry.set_context_provider(fake_context_provider)
        ctx = fake_context_provider()
        chart_colors = _make_chart_colors_from_context(ctx)

        assert chart_colors["accent"].name() == "#ff6600"
        assert chart_colors["font_family"] == "Arial"
        assert chart_colors["font_size"] == 15

        registry.reset()
