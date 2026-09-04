"""HoverPreviewOverlay / HoverPreviewController 单测（offscreen 状态机 + 几何）"""

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QWidget

from app.widgets.sidebar_hover_preview import HoverPreviewOverlay, HoverPreviewController


@pytest.fixture
def window(qtbot):
    w = QWidget()
    w.resize(1200, 800)
    qtbot.addWidget(w)
    return w


def test_overlay_is_native_and_no_activate(window, qtbot):
    ov = HoverPreviewOverlay(window, side="right", titlebar_h=38)
    qtbot.addWidget(ov)
    assert ov.testAttribute(Qt.WA_NativeWindow) is True
    assert ov.testAttribute(Qt.WA_ShowWithoutActivating) is True
    assert ov.testAttribute(Qt.WA_Hover) is True


def test_place_right_insets_edge(window, qtbot):
    ov = HoverPreviewOverlay(window, side="right", titlebar_h=38)
    qtbot.addWidget(ov)
    window.resize(1200, 800)
    ov.place(300)
    g = ov.geometry()
    assert abs(g.right() - (1200 - HoverPreviewOverlay.EDGE_INSET)) <= 1
    assert g.top() == 38
    assert g.height() == 800 - 38


def test_set_and_clear_content(window, qtbot):
    ov = HoverPreviewOverlay(window, side="right", titlebar_h=38)
    qtbot.addWidget(ov)
    frame = QWidget()
    qtbot.addWidget(frame)
    ov.set_content(frame)
    assert frame.parent() is ov
    ov.clear_content()
    # clear 不 setParent(None)：parent 仍是 ov，交调用方 reparent
    assert frame.parent() is ov


def _mk(window, qtbot, *, can=True, delay=300):
    ov = HoverPreviewOverlay(window, side="right", titlebar_h=38)
    qtbot.addWidget(ov)
    events = []
    ctrl = HoverPreviewController(
        ov,
        can_preview=lambda: can,
        on_enter=lambda: events.append("enter"),
        on_leave=lambda: events.append("leave"),
        hide_delay_ms=delay,
    )
    return ctrl, events


def test_hover_shows_immediately_when_collapsed(window, qtbot):
    ctrl, events = _mk(window, qtbot, can=True)
    ctrl.on_button_hover(True)
    assert events == ["enter"]
    assert ctrl.is_previewing() is True


def test_no_preview_when_already_expanded(window, qtbot):
    ctrl, events = _mk(window, qtbot, can=False)
    ctrl.on_button_hover(True)
    assert events == []
    assert ctrl.is_previewing() is False


def test_leave_then_reenter_cancels_hide(window, qtbot):
    ctrl, events = _mk(window, qtbot, delay=50)
    ctrl.on_button_hover(True)
    ctrl.on_button_hover(False)
    ctrl.on_overlay_hover(True)
    ctrl.on_overlay_hover(False)
    ctrl.on_button_hover(True)
    ctrl._hide_timer.stop()
    # 首次 hover(True) 进入预览，后续 hover(True) 只取消缓收计时；
    # 多次进出若 timer 一直未 timeout，leave 不触发。
    assert events.count("enter") == 1
    assert "leave" not in events


def test_leave_fires_after_delay(window, qtbot):
    ctrl, events = _mk(window, qtbot, delay=30)
    ctrl.on_button_hover(True)
    ctrl.on_button_hover(False)
    assert events == ["enter"]
    qtbot.wait(120)
    assert events == ["enter", "leave"]
    assert ctrl.is_previewing() is False


def test_clicked_cancels_preview_to_embed(window, qtbot):
    ctrl, events = _mk(window, qtbot, delay=300)
    ctrl.on_button_hover(True)
    ctrl.on_clicked()
    assert events == ["enter", "leave"]
    assert ctrl.is_previewing() is False
