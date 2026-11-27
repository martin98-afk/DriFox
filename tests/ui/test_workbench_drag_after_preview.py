"""工作台悬停预览回挂后，新的 splitter 把手也要保护聊天正文。"""
import pytest
from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import QApplication


@pytest.mark.ui
def test_workbench_drag_after_hover_preview(ui_app, qtbot):
    from app.widgets.message_card import MessageCard

    tm, mw = ui_app['tm'], ui_app['chat_window']
    tm.resize(1400, 750)
    tm.set_workbench_visible(False, animate=False)
    qtbot.wait(400)
    tm._wb_preview_enter()
    qtbot.wait(350)
    tm._wb_promote_on_leave = True
    tm._wb_preview_leave()
    qtbot.wait(650)
    card = MessageCard(role='assistant')
    mw.chat_layout.addWidget(card)
    card.set_content('工作台预览回挂后拖动分隔线，正文应保持显示。' * 20)
    card.ensure_rendered()
    qtbot.waitUntil(lambda: card.viewer._is_js_ready and card.viewer.isVisible())
    mw.chat_scroll_area.ensureWidgetVisible(card)
    qtbot.wait(300)
    h = tm._splitter.handle(tm._splitter.indexOf(tm._workbench_frame))
    origin = h.mapToGlobal(h.rect().center())
    qtbot.mousePress(h, Qt.LeftButton, pos=h.mapFromGlobal(origin))
    try:
        dest = origin - QPoint(60, 0)
        QApplication.sendEvent(h, QMouseEvent(QEvent.MouseMove, QPointF(h.mapFromGlobal(dest)), QPointF(dest), Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
        qtbot.wait(20)
        assert card.viewer.isVisible() and not card.resize_placeholder.isVisible()
    finally:
        qtbot.mouseRelease(h, Qt.LeftButton)
    assert not mw._sidebar_drag_live
