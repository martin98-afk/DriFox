"""在真实聊天列表中，手动折叠上方分组应保留标题的阅读位置。"""
import pytest
from PyQt5.QtCore import QEvent, QObject, QPoint, Qt
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

        bar.setValue(bar.value() + header_y() - 120)
        assert 0 <= header_y() < area.viewport().height() // 2
        initial_y = header_y()
        collapsed = viewer.height()
        target = viewer.focusProxy()
        viewer._run_js_sync("""window.clickTrace=[];for(let type of ['click','mousedown','mouseup','dblclick']) document.addEventListener(type,e=>clickTrace.push([type,e.clientY,e.target.className]),true)""")
        qtbot.mouseClick(target, Qt.LeftButton, pos=header)
        qtbot.waitUntil(lambda: viewer.height() > collapsed + 10)
        qtbot.wait(350)
        assert abs(header_y() - initial_y) <= 1
        bar.setValue(bar.maximum())
        bar.setValue(bar.value() + header_y() - initial_y)
        positions = []
        bar.valueChanged.connect(positions.append)
        avatar = card._identity_header._avatar
        viewport = area.viewport()
        initial_avatar_y = avatar.mapTo(viewport, QPoint()).y()
        frames = []

        class Watch(QObject):
            def eventFilter(self, obj, event):
                if event.type() == QEvent.Paint:
                    frames.append((avatar.mapTo(viewport, QPoint()).y(), card.height(), viewer.height()))
                return False

        watch = Watch()
        widgets = (card, avatar, mw.chat_container, viewport, viewer)
        for widget in widgets:
            widget.installEventFilter(watch)
        try:
            qtbot.mouseClick(target, Qt.LeftButton, pos=header)
            qtbot.wait(120)
            print("probe", viewer.height(), collapsed, viewer._run_js_sync("""[clickTrace,window.scrollY,Array.from(document.querySelectorAll('.dfx-group-button')).map(b=>[b.getAttribute('aria-expanded'), b.getBoundingClientRect().top]),document.body.scrollHeight]"""))
            qtbot.waitUntil(lambda: abs(viewer.height() - collapsed) <= 2)
            qtbot.wait(400)
            assert not positions, positions
            assert abs(header_y() - initial_y) <= 1
            assert frames and all(abs(y - initial_avatar_y) <= 1 for y, _, _ in frames), sorted(set(frames))
            assert all(h >= card.height() - 1 and vh >= collapsed - 1 for _, h, vh in frames), sorted(set(frames))
        finally:
            for widget in widgets:
                widget.removeEventFilter(watch)
            bar.valueChanged.disconnect(positions.append)
    finally:
        mw.chat_layout.removeWidget(card)
        card.hide()
        card.deleteLater()
