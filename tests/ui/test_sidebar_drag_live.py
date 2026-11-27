"""回归：左右侧栏手动拖拽时，正文不能切为占位或因中途停顿而落定。"""
import pytest
from PyQt5.QtCore import Qt


@pytest.mark.ui
@pytest.mark.parametrize("side", ["left", "right"])
def test_manual_drag_keeps_viewer_live_until_release(ui_app, qtbot, side):
    from app.widgets.message_card import MessageCard

    tm, mw = ui_app["tm"], ui_app["chat_window"]
    tm.resize(1400 if side == "right" else 1100, 720)
    tm.set_workbench_visible(side == "right", animate=False)
    qtbot.wait(600)
    card = MessageCard(role="assistant")
    mw.chat_layout.addWidget(card)
    card.set_content("拖动期间保持正文可读。" * 100)
    card.ensure_rendered()
    qtbot.waitUntil(lambda: card.viewer._is_js_ready and card.viewer.isVisible(), timeout=10000)
    mw.chat_scroll_area.ensureWidgetVisible(card)
    mw._do_debounced_resize()
    qtbot.wait(300)
    splitter = tm._splitter
    index = splitter.indexOf(tm._workbench_frame if side == "right" else tm._chat_frame)
    handle = splitter.handle(index)
    assert handle.isVisible()
    initial_width = card.width()
    origin = handle.x()
    direction = -1 if side == "right" else 1
    qtbot.mousePress(handle, Qt.LeftButton)
    try:
        # 沿用真实 QSplitter 移动/布局/信号路径，保留按键直到反向拖动完成。
        for delta in (12, 24, 36, 48, 60, 72):
            splitter.moveSplitter(origin + direction * delta, index)
            qtbot.wait(8)
            assert card.viewer.isVisible() and not card.resize_placeholder.isVisible()
        top = mw.chat_scroll_area.verticalScrollBar().value()
        assert card.geometry().bottom() >= top and card.geometry().top() < top + mw.chat_scroll_area.viewport().height()
        assert card.width() != initial_width, "持续拖动期间视口内正文宽度必须跟随"
        qtbot.wait(500)
        assert mw._sidebar_drag_live, "按住停顿不能被当成松手"
        assert tm._tab_panel._dragging_splitter == (side == "left"), "右侧拖动不能接管左侧折叠"
        splitter.moveSplitter(origin + direction * 36, index)
        assert card.viewer.isVisible() and not card.resize_placeholder.isVisible()
    finally:
        qtbot.mouseRelease(handle, Qt.LeftButton)
    qtbot.waitUntil(lambda: not mw._sidebar_drag_live and not tm._tab_panel._dragging_splitter)
    qtbot.wait(300)
    assert card.viewer.isVisible() and not card.resize_placeholder.isVisible()
