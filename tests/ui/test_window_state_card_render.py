"""窗口状态切换期间，消息正文不应被主动替换为缩放占位。"""
import pytest
from PyQt5.QtCore import QEvent, QObject


@pytest.mark.ui
@pytest.mark.parametrize('action', ['maximize', 'restore_max', 'restore_min'])
def test_window_state_keeps_message_painted(ui_app, qtbot, action):
    from app.widgets.message_card import MessageCard
    tm, mw = ui_app['tm'], ui_app['chat_window']
    tm.showNormal()
    tm.resize(1000, 680)
    if action == 'restore_max':
        tm.showMaximized()
    qtbot.wait(800)
    card = MessageCard(role='assistant')
    mw.chat_layout.addWidget(card)
    card.set_content('切换窗口状态时应一直看到这段正文。' * 35)
    card.ensure_rendered()
    qtbot.waitUntil(lambda: card.viewer._is_js_ready and card.viewer.isVisible())
    mw.chat_scroll_area.ensureWidgetVisible(card)
    qtbot.wait(300)
    if action == 'restore_min':
        tm.showMinimized()
        qtbot.wait(300)
    entered = []
    class Watch(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Show:
                entered.append(True)
            return False
    watch = Watch()
    card.resize_placeholder.installEventFilter(watch)
    frozen = []
    try:
        (tm.showMaximized if action == 'maximize' else tm.showNormal)()
        for _ in range(45):
            frozen.append(not tm._content_area.updatesEnabled())
            qtbot.wait(8)
        assert not entered, f'{action}: 正文被换为占位'
        assert not any(frozen), f'{action}: 内容区冻结了 {sum(frozen)} 个采样点'
        assert card.viewer.isVisible()
    finally:
        card.resize_placeholder.removeEventFilter(watch)
        mw.chat_layout.removeWidget(card)
        card.hide()
        card.deleteLater()
        tm.showNormal()
