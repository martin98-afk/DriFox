# -*- coding: utf-8 -*-
"""设置卡：授权失效必须能盖过「已关闭」显示

背景：关闭的 OAuth MCP（figma / notion / tencent-docs 默认 enabled=false）
在连接表里没有条目，设置卡一律画「已关闭」。探活补上状态之后，若刷新逻辑
仍按配置开关先判，用户永远看不到失效——本测试锁定分支顺序。
"""

import sys

import pytest
from PySide6.QtWidgets import QApplication

from app.tools.mcp_tools import MCPState
from app.widgets.cards.settings import mcp_setting_card as mod


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    _ensure_qapp()


def test_auth_expired_has_style():
    assert MCPState.AUTH_EXPIRED in mod._STATUS_STYLE
    color, tip = mod._STATUS_STYLE[MCPState.AUTH_EXPIRED]
    assert tip == "授权失效"
    assert color.startswith("#")


class _FakeDot:
    def set_color(self, _color):
        pass

    def setToolTip(self, _tip):
        pass


def _row_with_status():
    row = mod.MCPServerRow.__new__(mod.MCPServerRow)
    row._status_dot = _FakeDot()
    row._current_state = MCPState.DISABLED
    return row


def test_set_status_auth_expired_uses_error_as_tooltip():
    row = _row_with_status()
    row.set_status(MCPState.AUTH_EXPIRED, "授权已过期")
    assert row._current_state == MCPState.AUTH_EXPIRED


def test_refresh_dots_keeps_auth_expired_for_disabled_server():
    """配置里 enabled=false 也不能把「授权失效」盖成「已关闭」。"""
    from types import SimpleNamespace

    class _Mgr:
        @staticmethod
        def get_status():
            return [
                {
                    "name": "figma",
                    "state": MCPState.AUTH_EXPIRED,
                    "error": "授权已过期",
                    "tool_count": 0,
                }
            ]

    card = mod.MCPListSettingCard.__new__(mod.MCPListSettingCard)
    card.cfg = SimpleNamespace(mcp_enabled=SimpleNamespace(value=True))
    card._server_rows = {"figma": _row_with_status()}
    card._get_mcp_manager = lambda: _Mgr()
    card._get_servers = lambda: [{"name": "figma", "enabled": False}]

    card._refresh_status_dots()

    assert card._server_rows["figma"]._current_state == MCPState.AUTH_EXPIRED
