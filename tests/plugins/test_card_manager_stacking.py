# -*- coding: utf-8 -*-
"""CardManager dock 堆叠：多卡可见列表 + active 标记 + 非堆叠回退互斥"""

import pytest

from app.widgets.cards.card_manager import CardManager, ContainerType


class _FakeWidget:
    def __init__(self, stack: bool = False):
        self._stack = stack
        self.shown = 0

    def show_card(self):
        self.shown += 1

    def setVisible(self, v):
        pass

    def property(self, name):
        return self._stack if name == "stackInDock" else None

    def windowTitle(self):
        return "fake"


@pytest.fixture()
def cm():
    CardManager.reset_instance()
    yield CardManager.get_instance()
    CardManager.reset_instance()


def _register(cm, window, card_id, container, widget):
    cm._ensure_window_initialized(window)
    cm._window_data[window]["containers"][card_id] = container
    cm._window_data[window]["cards"][container][card_id] = widget


class TestDockStacking:
    def test_stackable_cards_coexist(self, cm):
        w1, w2 = _FakeWidget(stack=True), _FakeWidget(stack=True)
        _register(cm, "w", "c1", ContainerType.LEFT, w1)
        _register(cm, "w", "c2", ContainerType.LEFT, w2)
        cm.show_card("c1", "w")
        cm.show_card("c2", "w")
        visible = cm.get_visible_cards("w", ContainerType.LEFT)
        assert set(visible) == {"c1", "c2"}
        assert cm._window_data["w"]["dock_active_cards"][ContainerType.LEFT] == "c2"

    def test_non_stackable_exclusive(self, cm):
        """非堆叠卡 show 进 dock：清空可见列表回退单卡互斥（旧行为）"""
        w1, w2 = _FakeWidget(stack=True), _FakeWidget(stack=False)
        _register(cm, "w", "c1", ContainerType.LEFT, w1)
        _register(cm, "w", "c2", ContainerType.LEFT, w2)
        cm.show_card("c1", "w")
        cm.show_card("c2", "w")
        assert cm.get_visible_cards("w", ContainerType.LEFT) == ["c2"]

    def test_set_active_card(self, cm):
        """set_active_card 切换栈顶不触发 show/hide"""
        w1, w2 = _FakeWidget(stack=True), _FakeWidget(stack=True)
        _register(cm, "w", "c1", ContainerType.LEFT, w1)
        _register(cm, "w", "c2", ContainerType.LEFT, w2)
        cm.show_card("c1", "w")
        cm.show_card("c2", "w")
        cm.set_active_card("c1", "w")
        assert cm._window_data["w"]["dock_active_cards"][ContainerType.LEFT] == "c1"
        assert set(cm.get_visible_cards("w", ContainerType.LEFT)) == {"c1", "c2"}

    def test_is_card_stackable(self, cm):
        _register(cm, "w", "c1", ContainerType.LEFT, _FakeWidget(stack=True))
        _register(cm, "w", "c2", ContainerType.LEFT, _FakeWidget(stack=False))
        assert cm.is_card_stackable("c1", "w") is True
        assert cm.is_card_stackable("c2", "w") is False

    def test_hide_removes_from_visible_list(self, cm):
        w1, w2 = _FakeWidget(stack=True), _FakeWidget(stack=True)
        _register(cm, "w", "c1", ContainerType.LEFT, w1)
        _register(cm, "w", "c2", ContainerType.LEFT, w2)
        cm.show_card("c1", "w")
        cm.show_card("c2", "w")
        cm.hide_card("c2", "w")
        assert cm.get_visible_cards("w", ContainerType.LEFT) == ["c1"]
        assert cm._window_data["w"]["dock_active_cards"][ContainerType.LEFT] == "c1"
