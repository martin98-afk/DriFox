"""在真实聊天列表中，手动折叠上方分组应保留标题的阅读位置。"""
import pytest
from PyQt5.QtCore import QPoint, Qt
from tests.perf.conftest import compact_output_plugin  # noqa: F401


@pytest.mark.ui
@pytest.mark.usefixtures("compact_output_plugin")
def test_upper_group_keeps_reading_position(ui_app, qtbot):
    from app.widgets.message_card import MessageCard

    mw = ui_app["chat_window"]
    area = mw.chat_scroll_area
    bar = area.verticalScrollBar()
    qtbot.wait(900)
    card = MessageCard("assistant")
    try:
        mw._add_chat_widget(card)
        card.ensure_rendered()
        assert card._output_mode.id == "compact"
        qtbot.waitUntil(lambda: card.viewer._is_js_ready)
        card.start_streaming_anim()
        card.append_reasoning("这是第一段思考。" * 30)
        card.append_text("这里是第一段正文。\n\n" * 65)
        card.start_new_thinking_block()
        card.append_reasoning("这是第二段思考。")
        card.append_text("这里是第二段正文。\n\n")
        card.finish_streaming()
        qtbot.waitUntil(lambda: bar.maximum() > 300)
        qtbot.wait(750)
        viewer = card.viewer
        header = QPoint(65, 17)

        def header_y():
            return viewer.mapTo(area.viewport(), header).y()

        bar.setValue(bar.value() + header_y() - 50)
        assert 0 <= header_y() < area.viewport().height() // 2
        initial_y = header_y()
        collapsed = viewer.height()
        target = viewer.focusProxy()
        qtbot.mouseClick(target, Qt.LeftButton, pos=header)
        qtbot.waitUntil(lambda: viewer.height() > collapsed + 10)
        qtbot.wait(350)
        assert abs(header_y() - initial_y) <= 1
        bar.setValue(bar.maximum())
        bar.setValue(bar.value() + header_y() - initial_y)
        positions = []
        bar.valueChanged.connect(positions.append)
        try:
            qtbot.mouseClick(target, Qt.LeftButton, pos=header)
            qtbot.waitUntil(lambda: abs(viewer.height() - collapsed) <= 2)
            qtbot.wait(400)
            assert not positions, positions
            assert abs(header_y() - initial_y) <= 1
        finally:
            bar.valueChanged.disconnect(positions.append)
    finally:
        mw.chat_layout.removeWidget(card)
        card.hide()
        card.deleteLater()
