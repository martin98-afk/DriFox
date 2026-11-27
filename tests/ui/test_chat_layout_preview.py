"""非拖窗布局变化：高度变化和聊天限宽切换不应隐藏已绘制的正文。"""
import pytest
from PyQt5.QtCore import QObject, QEvent


@pytest.mark.ui
@pytest.mark.parametrize('case', ['width_preset', 'bottom_dock', 'window_width'])
def test_chat_layout_does_not_replace_body(ui_app, qtbot, monkeypatch, case):
    from app.widgets import tab_manager_window as module
    from app.widgets.message_card import MessageCard
    tm, mw = ui_app['tm'], ui_app['chat_window']
    tm.resize(1450, 740)
    tm.set_workbench_visible(False, animate=False)
    qtbot.wait(900)
    card = MessageCard(role='assistant')
    mw.chat_layout.addWidget(card)
    card.set_content('布局变化时保持消息正文。' * 30)
    card.ensure_rendered()
    qtbot.waitUntil(lambda: card.viewer._is_js_ready and card.viewer.isVisible())
    mw.chat_scroll_area.ensureWidgetVisible(card)
    qtbot.wait(400)
    shown = []
    class Watch(QObject):
        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.Show:
                shown.append(True)
            return False
    watch = Watch()
    card.resize_placeholder.installEventFilter(watch)
    old_size = mw.size()
    old_card_width = card.width()
    dock = tm._global_bottom_container
    was_hidden = dock.isHidden()
    try:
        if case == 'width_preset':
            monkeypatch.setattr(module, '_current_chat_width', lambda: 820)
            tm._on_chat_width_changed()
        elif case == 'window_width':
            tm.resize(900, tm.height())
        else:
            dock.show()
            total = sum(tm._chat_vsplitter.sizes())
            tm._chat_vsplitter.setSizes([total - 180, 180])
        qtbot.wait(350)
        assert mw.size() != old_size, '操作没有改变聊天区尺寸'
        if case == 'window_width':
            assert shown, '横向调整窗口仍应使用原有占位节流'
        else:
            if case == 'width_preset':
                assert card.width() < old_card_width, '卡片应按新限宽换行'
            else:
                dock.hide()
                qtbot.wait(350)
            assert not shown, f'{case}: 正文被换成占位'
    finally:
        card.resize_placeholder.removeEventFilter(watch)
        mw.chat_layout.removeWidget(card)
        card.hide()
        card.deleteLater()
        dock.setVisible(not was_hidden)
        monkeypatch.undo()
        tm._on_chat_width_changed()
