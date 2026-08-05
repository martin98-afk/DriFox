# -*- coding: utf-8 -*-
"""simple_hover_tooltip._HoverTooltipFilter 显示前鼠标校验测试（问题B 防御性修复）

覆盖：
① parent 隐藏 → _on_timeout 停表且不显示气泡
② 鼠标已移出 parent → 停表且不显示气泡
③ 鼠标仍在 parent 内 → 正常显示气泡
"""

from unittest.mock import patch

import pytest
from PyQt5.QtCore import QPoint
from PyQt5.QtWidgets import QLabel


def _make_label_with_filter(qtbot):
    """构造 QLabel 并通过 setToolTip 触发 monkey-patch 自动安装 filter

    ⚠️ 必须先 import 模块再 setToolTip：模块首次导入时才执行
    QWidget.setToolTip monkey-patch；若先 setToolTip（原生版本）则不会
    自动安装 filter。
    """
    from app.widgets.simple_hover_tooltip import _filters

    label = QLabel("target")
    qtbot.addWidget(label)
    label.setToolTip("测试提示")

    f = _filters.get(id(label))
    assert f is not None, "setToolTip 应自动安装 _HoverTooltipFilter"
    return label, f


def test_on_timeout_skips_when_parent_hidden(qtbot):
    """问题B：parent 未显示 → _on_timeout 停表且不创建气泡"""
    label, f = _make_label_with_filter(qtbot)
    with (
        patch.object(f, "_timer") as m_timer,
        patch.object(f, "_get_tooltip") as m_get,
    ):
        f._on_timeout()
    m_timer.stop.assert_called_once()
    m_get.assert_not_called()


def test_on_timeout_skips_when_cursor_outside(qtbot):
    """问题B：鼠标已移出 parent → 停表且不创建气泡"""
    label, f = _make_label_with_filter(qtbot)
    label.show()
    label.resize(100, 40)
    # 鼠标取远离 label 的屏幕坐标（用 label 全局几何外推）
    outside_global = label.mapToGlobal(QPoint(label.width() + 500, label.height() + 500))
    with (
        patch("app.widgets.simple_hover_tooltip.QCursor.pos", return_value=outside_global),
        patch.object(f, "_timer") as m_timer,
        patch.object(f, "_get_tooltip") as m_get,
    ):
        f._on_timeout()
    m_timer.stop.assert_called_once()
    m_get.assert_not_called()


def test_on_timeout_shows_when_cursor_inside(qtbot):
    """问题B：鼠标仍在 parent 内 → 正常显示气泡"""
    label, f = _make_label_with_filter(qtbot)
    label.show()
    label.resize(100, 40)
    inside_global = label.mapToGlobal(QPoint(10, 10))
    with (
        patch("app.widgets.simple_hover_tooltip.QCursor.pos", return_value=inside_global),
        patch.object(f, "_get_tooltip") as m_get,
    ):
        f._on_timeout()
    m_get.assert_called_once()


def test_event_filter_enter_starts_timer(qtbot):
    """回归：真实 QEnterEvent 走 eventFilter → tooltip 计时激活"""
    from PyQt5.QtCore import QPointF
    from PyQt5.QtGui import QEnterEvent
    from PyQt5.QtWidgets import QApplication, QPushButton

    from app.widgets.simple_hover_tooltip import _filters, install_hover_tooltip

    btn = QPushButton("btn")
    qtbot.addWidget(btn)
    # 带 text 调用时 setToolTip 内部已自动安装 filter 并短路返回 None，
    # 因此统一从注册表取 filter（公开 API 行为兼容两种路径）
    install_hover_tooltip(btn, "测试提示")
    f = _filters.get(id(btn))
    assert f is not None, "install_hover_tooltip 应注册 filter"

    QApplication.sendEvent(btn, QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(1, 1)))
    assert f._timer.isActive(), "Enter 事件应启动 tooltip 计时"


def test_event_filter_hide_to_parent_hides_tooltip(qtbot):
    """B2 回归：目标随父容器隐藏（HideToParent 事件）→ tooltip 隐藏

    团队 header 按钮（关闭团队 close_btn 等）在团队关闭时随 header 容器
    隐藏，Qt 发 HideToParent（27）而非 Hide（18）。旧分支只捕
    Hide/Leave/HoverLeave → tooltip 不隐藏 → 屏幕残留。
    """
    from unittest.mock import patch as _patch

    from PyQt5.QtCore import QEvent
    from PyQt5.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget

    from app.widgets.simple_hover_tooltip import _filters, install_hover_tooltip

    # 真实场景：按钮放入父容器（有 parent 时隐藏才发 HideToParent）
    container = QWidget()
    layout = QVBoxLayout(container)
    btn = QPushButton("btn")
    layout.addWidget(btn)
    qtbot.addWidget(container)
    install_hover_tooltip(btn, "关闭团队")
    f = _filters.get(id(btn))
    assert f is not None, "install_hover_tooltip 应注册 filter"

    # 模拟 tooltip 已显示
    with (
        _patch.object(f, "_hide") as m_hide,
        _patch.object(f, "_timer") as m_timer,
    ):
        # 父容器隐藏 → 子按钮收到 HideToParent 事件
        container.hide()
        # 事件已同步派发；直接补发一次验证 filter 分支（防御容器时序差异）
        QApplication.sendEvent(btn, QEvent(QEvent.HideToParent))
    m_hide.assert_called(), "HideToParent 事件应触发 _hide"
    m_timer.stop.assert_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
