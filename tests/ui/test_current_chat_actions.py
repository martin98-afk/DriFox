"""当前会话标题栏的归档、永久删除及取消确认。"""

import pytest
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QMenu, QPushButton

from app.widgets.common_dialogs import ConfirmDialog


@pytest.mark.ui
@pytest.mark.parametrize("action,confirm", [("归档", None), ("删除", "取消"), ("删除", "删除")])
def test_remove_current_chat(ui_session, qtbot, action, confirm):
    window = ui_session
    history = window.history_manager
    session = window.session_manager.get_current_session()
    sid = session.session_id
    session.set_messages([{"role": "user", "content": "菜单测试消息"}])
    history.save_session(messages=session.messages, session_id=sid, title="菜单测试")
    history.flush()
    assert history.get_pin_state(sid) is False

    def answer_dialog():
        dialog = next(d for d in window.window().findChildren(ConfirmDialog) if d.isVisible())
        button = next(b for b in dialog.findChildren(QPushButton) if b.text() == confirm)
        button.click()

    def select_action():
        menu = QApplication.activePopupWidget()
        assert isinstance(menu, QMenu)
        item = next(a for a in menu.actions() if a.text() == action)
        assert item.isEnabled()
        if confirm:
            QTimer.singleShot(100, answer_dialog)
        QTest.mouseClick(menu, Qt.LeftButton, pos=menu.actionGeometry(item).center())

    QTimer.singleShot(0, select_action)
    window._share_btn.click()
    history.flush()
    kept = confirm == "取消"
    assert (window.session_manager.get_current_session().session_id == sid) == kept
    assert (history.get_session_by_session_id(sid) is not None) == kept
    assert any(item["session_id"] == sid for item in history.get_archived_sessions()) == (action == "归档")
