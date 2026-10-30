# -*- coding: utf-8 -*<arg_value:6124c78e>
"""回归：左侧面板分隔线线色必须随主题刷新

根因：分隔线 QSS 曾写成类级 f-string `_SEPARATOR_STYLE`（模块 import 期求值一次），
DIVIDER_COLOR 深色主题是白色微透明、浅色主题是黑色微透明 —— 静态化把线色锁死在
首帧主题，切到对比色主题后线与底色同向，视觉上表现为「分隔线消失」。

判据：主题切换 + refresh_style() 之后，三条分隔线（底部 _separator、插件区
_plugin_separator_2、常驻区分隔线 _plugin_divider）的 QSS 必须含当前主题的
Colors.DIVIDER_COLOR，且不得含切换前的旧色。
"""

import re

import pytest
from PyQt5.QtWidgets import QFrame

from app.utils.design_tokens import Colors
from app.widgets.tab_panel import TabPanel

_BG_RE = re.compile(r"background:\s*([^;]+);")


def _line_color(qframe: QFrame) -> str:
    m = _BG_RE.search(qframe.styleSheet())
    assert m is not None, f"QSS 中未找到 background：{qframe.styleSheet()!r}"
    return m.group(1).strip()


@pytest.fixture
def panel(qtbot, monkeypatch):
    from unittest.mock import patch

    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    p.set_mode("list", persist=False)
    qtbot.addWidget(p)
    return p


def test_divider_qss_tracks_theme(panel, monkeypatch):
    """三条分隔线的线色随 Colors.DIVIDER_COLOR 变化（不被静态化锁死）"""
    # 常驻区分隔线只在「有 pin 项 + 有非 pin 项」时创建；这里直接注入一条，
    # 复用 _divider_qss 保证与真实重建路径同款样式。
    div = QFrame(panel._system_plugin_section)
    div.setFrameShape(QFrame.HLine)
    div.setStyleSheet(panel._divider_qss(4, 2))
    panel._plugin_divider = div

    before = Colors.DIVIDER_COLOR
    monkeypatch.setattr(Colors, "DIVIDER_COLOR", "rgba(1, 2, 3, 0.5)")
    try:
        panel.refresh_style()
        for name in ("_separator", "_plugin_separator_2", "_plugin_divider"):
            frame = getattr(panel, name)
            assert frame is not None
            assert _line_color(frame) == "rgba(1, 2, 3, 0.5)", f"{name} 未跟随主题刷新"
        # 旧色不得残留
        assert before not in panel._separator.styleSheet()
    finally:
        monkeypatch.undo()


def test_divider_margins_preserved(panel):
    """重刷不得吞掉原本的上下 margin（插件区 0/2、常驻区 2/2，底部那条无 margin）

    margin 值来源（2026-10-30 间隔均衡修正）：_plugin_separator_2 上方折叠头
    (margin-bottom 5) + 卡片 QSS margin-bottom 4 已贡献 9px，线顶给 0 才能与其
    下方 8px 持平；_plugin_divider 上下均为插件行（行内 2 + spacing 2），2/2 对称。
    """
    assert "margin-top: 0px" in panel._plugin_separator_2.styleSheet()
    assert "margin-bottom: 2px" in panel._plugin_separator_2.styleSheet()
    assert "margin-top: 0px" in panel._separator.styleSheet()
