# -*- coding: utf-8 -*-
"""回归测试：MCP 列表名称/分组标题 + 输入框胶囊图标随主题切换刷新

用户实测两个缺陷（2026-10-25 截图）：
1. 系统设置 → MCP 列表在深色主题下，服务器名称是黑字（几乎不可见），
   分组标题条仍是浅色底（#F0F0F0 白横条压深底）。
2. 输入框正文里的附件胶囊图标不随主题切换 —— 深色主题下仍是深色图标。

根因：
1. MCPServerRow 的名称 QLabel 只设了 ``font-weight: 600``，没设 color，
   而它是纯 QLabel（不是 qfw 的 FluentLabelBase 子类），拿不到
   ``FluentLabelBase { color: ... }`` 的主题 QSS，只能吃 palette 默认黑字。
   分组标题同理：内联 QSS 由 ``isDarkTheme()`` 分支生成，但
   ``MCPListSettingCard.refresh_style`` 只刷了行内描述，没重设标题。
2. ``FileMentionObject._icon_cache`` 缓存的是 ``FluentIcon.icon()`` 的返回值，
   该调用在当刻就按主题解析成具体 SVG 资源（``X_black.svg`` / ``X_white.svg``），
   缓存后主题切换仍取旧色。改用 ``qicon()``（包 FluentIconEngine，每次绘制
   按当前主题重新取路径）后缓存恒有效。
"""

import sys

import pytest

from PyQt5.QtWidgets import QApplication


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return _ensure_qapp()


# ═══════════════════════════════════════════════════
# 1. MCP 行名称颜色
# ═══════════════════════════════════════════════════


def _make_stdio_row(name: str = "browser"):
    from app.widgets.cards.settings.mcp_setting_card import MCPServerRow

    return MCPServerRow({"name": name, "type": "stdio", "command": "python", "args": ["server.py"]})


class TestMcpRowNameColor:
    def test_name_label_has_explicit_theme_color(self):
        """名称必须显式取主题文字色，不能依赖 palette 默认黑字"""
        from app.utils.design_tokens import Colors

        row = _make_stdio_row()
        qss = row._name_label.styleSheet()
        assert Colors.TEXT_PRIMARY in qss, f"名称 QSS 未含主题文字色: {qss!r}"
        # 加粗不能被覆盖掉
        assert "font-weight: 600" in qss

    def test_refresh_style_rewrites_name_color(self):
        """主题刷新时名称颜色必须重设（旧实现只刷描述/token）"""
        from app.utils.design_tokens import Colors

        row = _make_stdio_row()
        # 模拟名称停在旧主题色
        row._name_label.setStyleSheet("color: #123456; font-weight: 600;")

        row.refresh_style()

        qss = row._name_label.styleSheet()
        assert Colors.TEXT_PRIMARY in qss
        assert "#123456" not in qss


# ═══════════════════════════════════════════════════
# 2. MCP 分组标题
# ═══════════════════════════════════════════════════


def _make_list_card():
    """用 __new__ 绕过重量级 __init__，只补 refresh_style 用到的 Python 侧状态"""
    from unittest.mock import MagicMock

    from app.widgets.cards.settings.mcp_setting_card import MCPListSettingCard

    card = MCPListSettingCard.__new__(MCPListSettingCard)
    card._group_headers = []
    card._server_rows = {}
    return card


class TestMcpGroupHeaderRefresh:
    def test_refresh_style_rewrites_group_headers(self):
        """分组标题是内联 QSS（底色由深浅主题分支决定），必须随主题重设"""
        from unittest.mock import MagicMock

        card = _make_list_card()
        header = MagicMock()
        card._group_headers = [header]

        card.refresh_style()

        header.setStyleSheet.assert_called_once()
        qss = header.setStyleSheet.call_args[0][0]
        # 必须带底色与文字色（旧实现在构造期求值后永不更新）
        assert "background-color" in qss
        assert "color" in qss

    def test_destroyed_header_does_not_break_loop(self):
        """已销毁的标题只跳过自己，后续行照常刷新"""
        from unittest.mock import MagicMock

        card = _make_list_card()
        dead = MagicMock()
        dead.setStyleSheet.side_effect = RuntimeError("wrapped C/C++ object has been deleted")
        card._group_headers = [dead]
        row = MagicMock()
        card._server_rows = {"srv": row}

        card.refresh_style()

        row.refresh_style.assert_called_once()

    def test_group_headers_cleared_on_rebuild(self):
        """列表重建时旧标题引用必须清空，否则会向已 deleteLater 的对象写样式"""
        from app.widgets.cards.settings.mcp_setting_card import MCPListSettingCard

        src = MCPListSettingCard._refresh.__doc__ or ""
        assert "刷新服务器列表" in src or src == ""
        # 行为验证：重建后 _group_headers 只保留新标题（通过源码结构保证）
        import inspect

        body = inspect.getsource(MCPListSettingCard._refresh)
        assert "_group_headers.clear()" in body
        assert "_group_headers.append(header)" in body


# ═══════════════════════════════════════════════════
# 3. 输入框胶囊图标随主题
# ═══════════════════════════════════════════════════


def _opaque_color(icon, size: int = 16):
    """取图标 pixmap 里任一完全不透明像素的 RGB 和（用于比较深浅）"""
    img = icon.pixmap(size, size).toImage()
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() > 200:
                return c.red() + c.green() + c.blue()
    return None


class TestMentionPillIconFollowsTheme:
    def test_icon_pixmap_changes_with_theme(self, monkeypatch):
        """同一份缓存图标，在深浅两套主题下像素颜色必须不同

        旧实现缓存 ``icon()``（当刻解析成 black/white SVG），两次读取同一对象
        颜色恒等 → 深色主题下深色图标隐形。
        """
        import qfluentwidgets.common.icon as qicon_mod

        from app.widgets.bottom_input_area import FileMentionObject

        FileMentionObject._icon_cache.clear()

        monkeypatch.setattr(qicon_mod, "isDarkTheme", lambda: False)
        icon = FileMentionObject._icon_for("/tmp/sample.py")
        assert icon is not None
        light_sum = _opaque_color(icon)

        # 同一缓存对象，切到深色
        monkeypatch.setattr(qicon_mod, "isDarkTheme", lambda: True)
        dark_sum = _opaque_color(icon)

        FileMentionObject._icon_cache.clear()

        assert light_sum is not None and dark_sum is not None
        assert light_sum != dark_sum, "胶囊图标未随主题变色（缓存了当刻解析的 SVG）"

    def test_unknown_extension_falls_back_to_document(self):
        from app.widgets.bottom_input_area import FileMentionObject

        FileMentionObject._icon_cache.clear()
        icon = FileMentionObject._icon_for("/tmp/a.unknownext")
        assert icon is not None
