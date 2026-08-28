# -*- coding: utf-8 -*-
"""simple_hover_tooltip._HoverTooltipFilter 显示前鼠标校验测试（问题B 防御性修复）

覆盖：
① parent 隐藏 → _on_timeout 停表且不显示气泡
② 鼠标已移出 parent → 停表且不显示气泡
③ 鼠标仍在 parent 内 → 正常显示气泡
"""

from unittest.mock import patch

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QLabel


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
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QEnterEvent
    from PySide6.QtWidgets import QApplication, QPushButton

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

    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget

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


def test_event_filter_mouse_press_hides_tooltip(qtbot):
    """B3 回归：点击（MouseButtonPress/MouseButtonDblClick）→ tooltip 立即隐藏

    团队关闭按钮 hover 显示 tooltip 后点击关闭团队，若按下时 tooltip 不隐藏，
    团队组 deleteLater 延迟销毁（DeferredDelete 事件循环空闲才执行）期间
    tooltip 残留在屏幕上。与 Qt 原生 QToolTip / qfluentwidgets ToolTipFilter
    在 MouseButtonPress 时 hideToolTip() 的行为对齐。
    """
    from unittest.mock import patch as _patch

    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication, QPushButton

    from app.widgets.simple_hover_tooltip import _filters, install_hover_tooltip

    btn = QPushButton("btn")
    qtbot.addWidget(btn)
    install_hover_tooltip(btn, "关闭团队")
    f = _filters.get(id(btn))
    assert f is not None, "install_hover_tooltip 应注册 filter"

    with (
        _patch.object(f, "_hide") as m_hide,
        _patch.object(f, "_timer") as m_timer,
    ):
        QApplication.sendEvent(btn, QEvent(QEvent.MouseButtonPress))
    m_hide.assert_called(), "MouseButtonPress 应触发 _hide（tooltip 收起）"
    m_timer.stop.assert_called(), "MouseButtonPress 应停止 hover 计时"

    # 双击同样收起
    with (
        _patch.object(f, "_hide") as m_hide2,
        _patch.object(f, "_timer") as m_timer2,
    ):
        QApplication.sendEvent(btn, QEvent(QEvent.MouseButtonDblClick))
    m_hide2.assert_called(), "MouseButtonDblClick 应触发 _hide"
    m_timer2.stop.assert_called()


def test_guard_hides_when_cursor_leaves_after_show(qtbot):
    """残留修复（R1）回归：tooltip 显示后若光标离开目标（滚动/漏发 Leave），
    看护轮询应在 ~120ms 内强制收起，不残留。
    """

    from unittest.mock import patch as _patch

    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication, QLabel

    from app.widgets.simple_hover_tooltip import _filters

    # 用一个真实窗口顶替 activeWindow，避免无头环境下 activeWindow() 为 None
    # 导致看护提前收起、干扰“光标移出”这一断言语义。
    label = QLabel("target")
    qtbot.addWidget(label)
    label.show()
    label.resize(100, 40)
    label.setToolTip("测试提示")
    f = _filters.get(id(label))
    assert f is not None, "setToolTip 应自动安装 _HoverTooltipFilter"

    inside = label.mapToGlobal(QPoint(10, 10))
    outside = label.mapToGlobal(QPoint(label.width() + 500, label.height() + 500))
    with (
        _patch("app.widgets.simple_hover_tooltip.QCursor.pos", return_value=inside),
        _patch.object(QApplication, "activeWindow", return_value=label),
    ):
        f._on_timeout()
    assert f._tooltip is not None and f._tooltip.isVisible(), "光标在内应已显示"

    # 光标移到目标外部（模拟滚动/漏发 Leave），看护应强制收起
    with (
        _patch("app.widgets.simple_hover_tooltip.QCursor.pos", return_value=outside),
        _patch.object(QApplication, "activeWindow", return_value=label),
    ):
        qtbot.wait(300)
    assert not f._tooltip.isVisible(), "看护应在光标移出后强制收起 tooltip（不残留）"


def test_guard_hides_on_app_deactivate(qtbot):
    """残留修复（R1）回归：应用失焦（activeWindow=None，如 alt-tab）时
    tooltip 应被看护收起，不飘在其他窗口上。
    """

    from unittest.mock import patch as _patch

    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication, QLabel

    from app.widgets.simple_hover_tooltip import _filters

    label = QLabel("target")
    qtbot.addWidget(label)
    label.show()
    label.resize(100, 40)
    label.setToolTip("测试提示")
    f = _filters.get(id(label))
    assert f is not None, "setToolTip 应自动安装 _HoverTooltipFilter"

    inside = label.mapToGlobal(QPoint(10, 10))
    with (
        _patch("app.widgets.simple_hover_tooltip.QCursor.pos", return_value=inside),
        _patch.object(QApplication, "activeWindow", return_value=label),
    ):
        f._on_timeout()
    assert f._tooltip.isVisible(), "应已显示"

    # 应用失焦 → 看护应强制收起
    with _patch.object(QApplication, "activeWindow", return_value=None):
        qtbot.wait(300)
    assert not f._tooltip.isVisible(), "应用失焦时 tooltip 应被收起（不残留）"


def test_event_filter_mouse_press_does_not_block_click(qtbot):
    """B3 回归：MouseButtonPress 仅收起 tooltip，不拦截鼠标事件

    eventFilter 对 MouseButtonPress 必须返回 False（继续传播），否则按钮
    收不到按下事件，点击（clicked）失效。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPushButton

    from app.widgets.simple_hover_tooltip import _filters, install_hover_tooltip

    btn = QPushButton("btn")
    qtbot.addWidget(btn)
    install_hover_tooltip(btn, "关闭团队")
    f = _filters.get(id(btn))
    assert f is not None, "install_hover_tooltip 应注册 filter"

    clicked = []
    btn.clicked.connect(lambda: clicked.append(True))
    QTest.mouseClick(btn, Qt.LeftButton)
    assert clicked, "MouseButtonPress 不应被 filter 拦截，按钮点击应正常触发"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
