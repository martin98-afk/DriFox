"""等待流式首字时，不应周期性重绘静止的正文。"""

from PyQt5.QtTest import QSignalSpy

from app.widgets.message_card import MessageCard


def test_waiting_for_first_chunk_has_no_paint_timer(qtbot):
    card = MessageCard("assistant")
    qtbot.addWidget(card)
    card.show()
    ticks = QSignalSpy(card._anim_timer.timeout)
    card.start_streaming_anim()
    qtbot.wait(220)
    assert len(ticks) == 0
    assert not card._anim_timer.isActive()
    card.stop_streaming_anim()
