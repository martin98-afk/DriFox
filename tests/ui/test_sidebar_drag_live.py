"""回归：左侧栏手动拖拽时，正文不能切为占位或因中途停顿而落定。"""
import pytest
from PyQt5.QtCore import Qt


@pytest.mark.ui
def test_manual_drag_keeps_viewer_live_until_release(ui_app, qtbot):
    from app.widgets.message_card import MessageCard

    tm, mw = ui_app["tm"], ui_app["chat_window"]
    tm.resize(1100, 720)
    qtbot.wait(600)
    card = MessageCard(role="assistant")
    mw.chat_layout.insertWidget(0, card)
    card.set_content("拖动期间保持正文可读。" * 100)
    card.ensure_rendered()
    qtbot.waitUntil(lambda: card.viewer._is_js_ready and card.viewer.isVisible(), timeout=10000)
    mw.chat_scroll_area.verticalScrollBar().setValue(0)
    mw._do_debounced_resize()
    qtbot.wait(300)
    splitter = tm._splitter
    handle = splitter.handle(1)
    initial_width = card.width()
    left = splitter.sizes()[0]
    qtbot.mousePress(handle, Qt.LeftButton)
    try:
        # 沿用真实 QSplitter 移动/布局/信号路径，保留按键直到反向拖动完成。
        for delta in (12, 24, 36, 48, 60, 72):
            splitter.moveSplitter(left + delta, 1)
            qtbot.wait(8)
            assert card.viewer.isVisible() and not card.resize_placeholder.isVisible()
        assert card.width() != initial_width, "持续拖动期间正文宽度必须跟随，不能等静默期"
        qtbot.wait(500)
        assert tm._tab_panel._dragging_splitter, "按住停顿不能被当成松手"
        splitter.moveSplitter(left + 36, 1)
        assert card.viewer.isVisible() and not card.resize_placeholder.isVisible()
    finally:
        qtbot.mouseRelease(handle, Qt.LeftButton)
    qtbot.waitUntil(lambda: not tm._tab_panel._dragging_splitter)
    qtbot.wait(300)
    assert card.viewer.isVisible() and not card.resize_placeholder.isVisible()
