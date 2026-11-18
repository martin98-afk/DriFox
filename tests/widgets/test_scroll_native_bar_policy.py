# -*- coding: utf-8 -*-
"""回归：install_fast_wheel 换原生条后 policy setter 必须恢复原生语义。

根因（2026-11-18 侧边栏自定义插件区滚动条消失）：
qfw ``SmoothScrollDelegate.__init__`` 把实例方法 ``setVerticalScrollBarPolicy``
替换为自己的实现——内部用类方法强制原生条 AlwaysOff、只联动已隐藏的 qfw
悬浮条。``_switch_to_native_bar`` 换上原生条后，调用点随后任何一次
``setVerticalScrollBarPolicy(ScrollBarAsNeeded)``（各滚动区构造时都有）都会把
原生条打回永久隐藏（内容超高也只见得滚不见得条）。修复：换条后删除实例
劫持属性，policy setter 回到原生语义。
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QLabel, QScrollBar

from qfluentwidgets import ScrollArea

from app.utils.scroll_tuning import install_fast_wheel


def _make_area(qtbot) -> ScrollArea:
    area = ScrollArea()
    area.setWidgetResizable(True)
    qtbot.addWidget(area)
    inner = QLabel("x" * 200)
    inner.setFixedHeight(600)
    area.setWidget(inner)
    # 复现调用点顺序：先装 fast wheel（换原生条），再设策略（曾走劫持版）
    assert install_fast_wheel(area) is True
    area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    area.resize(200, 200)
    area.show()
    return area


def test_policy_setter_not_hijacked_after_native_bar_swap(qtbot):
    """劫持存在时 policy() 恒读出 AlwaysOff（劫持内部强制写入），修复后应如实反映调用点的 AsNeeded。"""
    area = _make_area(qtbot)
    assert area.verticalScrollBarPolicy() == Qt.ScrollBarAsNeeded


def test_native_bar_visible_when_content_overflow(qtbot):
    """内容超高 + AsNeeded：换条后的原生滚动条必须实际可见（bug 症状本体）。"""
    area = _make_area(qtbot)
    vbar = area.verticalScrollBar()
    assert isinstance(vbar, QScrollBar)  # 已是原生条（非 qfw SmoothScrollBar）
    assert vbar.maximum() > 0  # 内容确实超高
    assert vbar.isVisible()


def test_policy_setter_controls_native_bar(qtbot):
    """policy setter 直接作用于原生条本身：AlwaysOff → 隐藏。"""
    area = _make_area(qtbot)
    assert area.verticalScrollBar().isVisible()
    area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    assert not area.verticalScrollBar().isVisible()


def test_sidebar_scrollbar_policies(qtbot):
    """侧栏滚动条策略（2026-11-18 用户定案）。

    - 自定义插件区垂直条 AlwaysOn：恒显恒占位。换原生条后 AsNeeded 的出现/
      消失会让 viewport 宽度跳变 8px，行内容左右横移（用户感知为「横向移动
      的滚轮」）；恒占位压掉跳变，且条恒可见（用户诉求一「看不见」）。
    - 树区 / 对话列表区 AsNeeded：保持既有观感。
    - 三区横向条均 AlwaysOff：无横向滚动需求。
    """
    from unittest.mock import patch

    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        from app.widgets.tab_panel import TabPanel

        p = TabPanel()
    qtbot.addWidget(p)
    assert p._custom_plugin_scroll.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOn
    assert p._scroll_area.verticalScrollBarPolicy() == Qt.ScrollBarAsNeeded
    assert p._tree_scroll.verticalScrollBarPolicy() == Qt.ScrollBarAsNeeded
    for attr in ("_custom_plugin_scroll", "_scroll_area", "_tree_scroll"):
        area = getattr(p, attr)
        assert area.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff, attr
        assert not area.horizontalScrollBar().isVisible(), attr
