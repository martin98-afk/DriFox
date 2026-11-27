"""验证真实桌面合成后的聊天正文，而非仅检查 Qt 可见标志。"""
import pytest
from PyQt5.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PyQt5.QtGui import QImage, QMouseEvent
from PyQt5.QtWidgets import QApplication


@pytest.mark.ui
@pytest.mark.parametrize("action", ["open", "close", "drag"])
def test_workbench_keeps_painted_text(ui_app, qtbot, action):
    from app.widgets.message_card import MessageCard

    tm, mw = ui_app['tm'], ui_app['chat_window']
    screen = QApplication.primaryScreen()
    tm.resize(min(1200, screen.availableGeometry().width()), 700)
    tm.move(screen.availableGeometry().topLeft())
    tm.raise_()
    tm.activateWindow()
    tm.set_workbench_visible(action != 'open', animate=False)
    qtbot.wait(500)
    card = MessageCard(role='assistant')
    mw.chat_layout.addWidget(card)
    card.set_content('\n\n'.join(["<span style='color:#f00883'>█████████████████████████████████</span>"] * 6))
    card.ensure_rendered()
    qtbot.waitUntil(lambda: card.viewer._is_js_ready and card.viewer.isVisible(), timeout=10000)
    mw.chat_scroll_area.ensureWidgetVisible(card)
    mw._do_debounced_resize()
    qtbot.wait(400)
    vp = mw.chat_scroll_area.viewport()

    def painted():
        # 卡片内容的桌面坐标；即使 viewer 被隐藏仍读同一位置的屏幕像素。
        rect = QRect(card.viewer.mapToGlobal(QPoint()), card.viewer.size()).intersected(
            QRect(vp.mapToGlobal(QPoint()), vp.size())).intersected(screen.geometry())
        if rect.isEmpty():
            return 0
        shot = screen.grabWindow(0, rect.x(), rect.y(), min(rect.width(), 290), rect.height()).toImage().convertToFormat(QImage.Format_RGB32)
        # B,G,R,X；仅统计亮粉测试正文，卡片底色、阴影及右侧栏都不会被误计。
        ptr = shot.constBits()
        ptr.setsize(shot.byteCount())
        buf = bytes(ptr)
        return sum(buf[i+2] > 180 and buf[i+1] < 70 and 75 < buf[i] < 180 for i in range(0, len(buf)-3, 32))

    before = painted()
    assert before > 20, f'未取得可辨识正文像素：{before}'
    frames = []
    handle = tm._splitter_right_handle
    if action == 'drag':
        qtbot.mousePress(handle, Qt.LeftButton)
    else:
        tm.toggle_workbench()
    try:
        origin = handle.mapToGlobal(handle.rect().center())
        for i in range(32):
            if action == 'drag' and i < 12:
                dest = origin - QPoint(8 * (i if i < 8 else 16 - i), 0)
                move = QMouseEvent(QEvent.MouseMove, QPointF(handle.mapFromGlobal(dest)), QPointF(dest),
                                   Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
                QApplication.sendEvent(handle, move)
            elif action == 'drag' and i == 12:
                qtbot.mouseRelease(handle, Qt.LeftButton)
            qtbot.wait(8)
            frames.append(painted())
    finally:
        if action == 'drag' and mw._sidebar_drag_live:
            qtbot.mouseRelease(handle, Qt.LeftButton)
        mw.chat_layout.removeWidget(card)
        card.hide()
        card.deleteLater()
    assert min(frames) > before * 0.12, f'{action}: 正文像素基线={before}，逐帧={frames}'
