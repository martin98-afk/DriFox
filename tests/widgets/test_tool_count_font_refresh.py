# -*- coding: utf-8 -*-
"""回归：右下角工具计数 label 字体/颜色随设置切换刷新

2026-10-13：字体变更后 _on_settings_config_changed 的 5b 补刷块漏掉
_tool_count_label，其 QSS 内嵌旧 font-family 压制 setFont，输入框右下角
安全/危险计数不随新字体（主题切换时连颜色都停留旧值）。
"""

import inspect
import sys
from unittest.mock import MagicMock

import pytest


def test_settings_refresh_chain_covers_tool_count_label():
    """批量刷新体（5b 补刷块）必须包含 _refresh_tool_toggle_btn 调用（否则字体切换后计数停旧字体）"""
    from app.main_widget import OpenAIChatToolWindow

    src = inspect.getsource(OpenAIChatToolWindow._apply_runtime_ui_settings)
    assert "_refresh_tool_toggle_btn()" in src


def test_refresh_tool_toggle_btn_rebuilds_font_css():
    """_refresh_tool_toggle_btn 重设 label QSS 时嵌入当前缓存字族

    用 MagicMock 承载 label（pytest 进程内真 QLabel 曾触发 0xC0000005，
    与被测逻辑无关；独立脚本同路径已验证真 widget 行为一致）。
    """
    import app.utils.utils as utils_mod
    from app.main_widget import OpenAIChatToolWindow

    # 模拟字体切换后的缓存状态（不触碰用户真实配置）
    original = utils_mod._cached_font_family_css
    utils_mod._cached_font_family_css = "font-family: '黑体';"
    try:
        host = MagicMock()
        host._tool_permission_controller.get_toggles.return_value = {
            "read_file": True,
            "write_file": False,
        }
        host._tool_permission_controller.get_active_agent_name.return_value = None

        OpenAIChatToolWindow._refresh_tool_toggle_btn(host)

        css = host._tool_count_label.setStyleSheet.call_args[0][0]
        assert "font-family: '黑体';" in css
    finally:
        utils_mod._cached_font_family_css = original


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
