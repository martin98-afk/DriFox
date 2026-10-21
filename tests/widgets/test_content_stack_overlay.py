# -*- coding: utf-8 -*-
"""回归：_ContentStack 叠放化后语义等价 QStackedWidget + 覆盖层页保活不变式。

背景（2026-09-08 排查）：QStackedWidget 页切换触发覆盖层子树整树 show 传播
（实测 270-335ms）。叠放化后覆盖层页首次 show 永不 hide，切换只 raise/lower
+ hide/show 对话页（双向 <12ms）。
"""

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QLabel

from app.widgets.tab_manager_window import _ContentStack


@pytest.fixture
def stack(qtbot):
    s = _ContentStack()
    qtbot.addWidget(s)
    p0 = QLabel("chat")
    p1 = QLabel("overlay")
    s.addWidget(p0)
    s.addWidget(p1)
    s.resize(800, 600)
    s.show()
    return s, p0, p1


class TestContentStackOverlay:
    def test_initial_state(self, stack):
        s, p0, p1 = stack
        assert s.currentIndex() == 0
        assert s.currentWidget() is p0
        assert s.widget(0) is p0 and s.widget(1) is p1
        assert s.widget(9) is None

    def test_switch_to_overlay_and_back(self, stack):
        s, p0, p1 = stack
        s.setCurrentIndex(1)
        assert s.currentIndex() == 1
        assert s.currentWidget() is p1
        s.setCurrentIndex(0)
        assert s.currentIndex() == 0
        assert s.currentWidget() is p0

    def test_overlay_page_never_hidden_after_first_show(self, stack):
        """覆盖层页保活不变式：常驻 visible（压在对话页下），永不显式 hide（消灭 show 传播）"""
        s, p0, p1 = stack
        assert not p1.testAttribute(Qt.WA_WState_Hidden)  # 初始即 visible 在下（启动期子树为空，零成本）
        s.setCurrentIndex(1)
        assert not p1.testAttribute(Qt.WA_WState_Hidden)
        s.setCurrentIndex(0)  # 切回：不得 hide 覆盖层页
        assert not p1.testAttribute(Qt.WA_WState_Hidden)
        s.setCurrentIndex(1)
        s.setCurrentIndex(0)
        assert not p1.testAttribute(Qt.WA_WState_Hidden)

    def test_conversation_page_hidden_in_overlay_mode(self, stack):
        """覆盖层模式：对话页必须 hidden（无焦点穿透 + 省布局）"""
        s, p0, p1 = stack
        s.setCurrentIndex(1)
        assert p0.testAttribute(Qt.WA_WState_Hidden)
        s.setCurrentIndex(0)
        assert not p0.testAttribute(Qt.WA_WState_Hidden)

    def test_same_index_noop(self, stack):
        s, p0, p1 = stack
        fired = []
        s.currentChanged.connect(fired.append)
        s.setCurrentIndex(0)  # 与当前相同
        assert fired == []

    def test_current_changed_signal(self, stack):
        s, p0, p1 = stack
        fired = []
        s.currentChanged.connect(fired.append)
        s.setCurrentIndex(1)
        assert fired == [1]

    def test_min_hint_disabled(self, stack):
        s, _p0, _p1 = stack
        s.set_min_hint_disabled(True)
        hint = s.minimumSizeHint()
        assert hint.width() == 0 and hint.height() == 0
        s.set_min_hint_disabled(False)
        s.set_min_hint_disabled(False)  # 幂等
        assert not s.minimumSizeHint().isNull()
