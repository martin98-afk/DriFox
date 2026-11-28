"""真实消息卡片内的任务折叠：验证中间帧、反向、动态增行与减少动效。"""
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QVBoxLayout, QWidget

from app.utils.design_tokens import Animations
from app.widgets.message_card import MessageCard


def _todos(n):
    return [{"content": f"测试任务 {i}", "status": "pending"} for i in range(n)]


def test_real_card_disclosure_frames_and_reversal(qapp, monkeypatch):
    monkeypatch.setattr(Animations, "motion_enabled", staticmethod(lambda: True))
    host = QWidget()
    layout = QVBoxLayout(host)
    card = MessageCard(role="assistant", model_name="test-model")
    layout.addWidget(card)
    layout.addStretch(1)
    host.resize(640, 720)
    host.show()
    try:
        card.update_todo_list(_todos(6))
        QTest.qWait(20)
        panel = card._todo_panel
        collapsed = card.height()
        QTest.keyClick(panel._collapse_btn, Qt.Key_Return)
        QTest.qWait(65)
        mid = card.height()
        assert mid > collapsed
        cap = panel._list_wrap.maximumHeight()
        assert 0 < cap < panel._list_wrap.sizeHint().height()
        QTest.keyClick(panel._collapse_btn, Qt.Key_Space)
        assert abs(panel._list_wrap.maximumHeight() - cap) <= 1
        QTest.qWait(Animations.EXIT_MS + 60)
        assert card.height() == collapsed
        panel._collapse_btn.click()
        QTest.qWait(50)
        card.update_todo_list(_todos(10))
        QTest.qWait(Animations.ENTER_MS + 80)
        assert card.height() > mid
        assert panel._list_wrap.height() >= panel._list_wrap.sizeHint().height()
        assert panel._list_wrap.maximumHeight() == 16777215
        monkeypatch.setattr(Animations, "motion_enabled", staticmethod(lambda: False))
        panel._collapse_btn.click()
        assert panel._list_wrap.isHidden()
    finally:
        host.close()
        host.deleteLater()
        qapp.processEvents()
