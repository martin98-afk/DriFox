"""聊天区缩窄时，右对齐的用户气泡不能被旧宽度的离屏消息推到视口外。"""
import pytest
from PyQt5.QtCore import QPoint, Qt


@pytest.mark.ui
@pytest.mark.parametrize('text', ['短消息', '缩窄时这段文字也要完整显示。' * 20])
def test_bubble_stays_in_view_during_shrink(ui_app, qtbot, text):
    from app.widgets.message_card import MessageCard
    tm, mw = ui_app['tm'], ui_app['chat_window']
    tm.resize(1250, 770)
    tm.set_workbench_visible(True, animate=False)
    qtbot.wait(750)
    cards = []
    try:
        for _ in range(5):
            c = MessageCard(role='assistant')
            mw.chat_layout.addWidget(c)
            cards.append(c)
            c.setMinimumHeight(240)
            mw._sync_single_card_width(c)
        c = MessageCard(role='user')
        mw.chat_layout.addWidget(c, 0, Qt.AlignRight)
        cards.append(c)
        c.set_content(text)
        c.ensure_rendered()
        mw._sync_single_card_width(c)
        mw.chat_scroll_area.ensureWidgetVisible(c)
        qtbot.wait(300)
        vp = mw.chat_scroll_area.viewport()
        bubble = c._user_bubble
        handle = tm._splitter.handle(tm._splitter.indexOf(tm._workbench_frame))
        origin = handle.x()
        start_width = vp.width()
        qtbot.mousePress(handle, Qt.LeftButton)
        try:
            for delta in (20, 55, 100, 180, 200, 130, 50, 0):
                tm._splitter.moveSplitter(origin - delta, tm._splitter.indexOf(tm._workbench_frame))
                qtbot.wait(8)
                x = bubble.mapTo(vp, QPoint()).x()
                assert 0 <= x and x + bubble.width() <= vp.width(), (x, bubble.width(), vp.width(), mw.chat_container.width(), delta)
                if delta == 180:
                    assert vp.width() < start_width
        finally:
            qtbot.mouseRelease(handle, Qt.LeftButton)
    finally:
        for c in cards:
            mw.chat_layout.removeWidget(c)
            c.hide()
            c.deleteLater()
