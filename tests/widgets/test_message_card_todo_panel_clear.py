# -*- coding: utf-8 -*-
"""T11-③：clear_todo_list 完备销毁守护测试

迁卡路径旧语义只 removeWidget 不销毁 → 面板 QWidget 级滞留；新语义顺序契约
对齐 remove_inline_panel：update_todos([]) → removeWidget → hide → setParent(None)
→ deleteLater，并清 _todo_panel/_todo_panel_height_cache（保留 _todo_panel_host）。
"""
import os
import sys

import pytest

sys.path.insert(0, ".")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt5 import sip
except ImportError:
    import sip
from PyQt5.QtCore import QCoreApplication, QEvent, QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

def _flush_delete_later(qapp, rounds: int = 20):
    """DeferredDelete 不走普通 processEvents，须 sendPostedEvents 显式派发"""
    for _ in range(rounds):
        qapp.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

from app.widgets.message_card import MessageCard  # noqa: E402

def _card_with_todo_panel():
    """轻量构造 MessageCard 并挂出真实 InlineTodoPanel"""
    card = MessageCard.__new__(MessageCard)
    QWidget.__init__(card)
    host = QVBoxLayout()
    card._todo_panel_host = host
    card._todo_panel = None
    card._todo_panel_height_cache = 0
    card._todo_panel_todos = []
    card._assistant_bubble = QWidget()
    card._assistant_bubble.setLayout(host)
    panel = card._ensure_todo_panel()
    panel.show()
    return card, panel

@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app

class TestClearTodoList:
    def test_clear_destroys_panel(self, qapp):
        card, panel = _card_with_todo_panel()
        assert card._todo_panel is panel
        card.clear_todo_list()
        assert card._todo_panel is None
        assert card._todo_panel_height_cache == 0
        assert card._todo_panel_host is not None, "宿主槽位保留供新卡片复用"
        _flush_delete_later(qapp)
        assert sip.isdeleted(panel), "面板 C++ 对象应经 deleteLater 销毁"

    def test_clear_idempotent(self, qapp):
        """连清两次不抛（第二次 _todo_panel 已 None 直接返回）"""
        card, _panel = _card_with_todo_panel()
        card.clear_todo_list()
        card.clear_todo_list()
        assert card._todo_panel is None
