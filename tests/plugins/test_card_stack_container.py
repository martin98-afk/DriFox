# -*- coding: utf-8 -*-
"""CardStackContainer：attach/sync/active 切换/空隐藏"""

import pytest

from app.widgets.cards.card_manager import CardManager, ContainerType

pytest.importorskip("PyQt5.QtWidgets")


@pytest.fixture()
def cm():
    CardManager.reset_instance()
    yield CardManager.get_instance()
    CardManager.reset_instance()


def _mk_cm_window(cm):
    cm._ensure_window_initialized("w")
    return cm


class TestCardStackContainer:
    def test_attach_and_sync(self, qapp, cm):
        _mk_cm_window(cm)
        from PyQt5.QtWidgets import QWidget

        from app.widgets.cards.card_stack_container import CardStackContainer

        stack = CardStackContainer()
        stack.set_container_context("w", ContainerType.LEFT)
        w1, w2 = QWidget(), QWidget()
        stack.attach_card("c1", w1)
        stack.attach_card("c2", w2)
        # manager 侧模拟两卡可见 + active=c2
        cm._window_data["w"]["dock_visible_cards"][ContainerType.LEFT] = ["c1", "c2"]
        cm._window_data["w"]["dock_active_cards"][ContainerType.LEFT] = "c2"
        stack.sync_from_manager()
        assert stack.count() == 2
        assert stack.active_card_id() == "c2"
        assert stack.isVisible() is True

    def test_active_switch(self, qapp, cm):
        _mk_cm_window(cm)
        from PyQt5.QtWidgets import QWidget

        from app.widgets.cards.card_stack_container import CardStackContainer

        stack = CardStackContainer()
        stack.set_container_context("w", ContainerType.LEFT)
        stack.attach_card("c1", QWidget())
        stack.attach_card("c2", QWidget())
        # 注册 card_id → container 映射（set_active_card 依赖）
        cm._window_data["w"]["containers"]["c1"] = ContainerType.LEFT
        cm._window_data["w"]["containers"]["c2"] = ContainerType.LEFT
        cm._window_data["w"]["dock_visible_cards"][ContainerType.LEFT] = ["c1", "c2"]
        cm._window_data["w"]["dock_active_cards"][ContainerType.LEFT] = "c2"
        stack.sync_from_manager()
        stack._on_pivot_changed("c1")
        assert stack.active_card_id() == "c1"
        assert cm._window_data["w"]["dock_active_cards"][ContainerType.LEFT] == "c1"

    def test_empty_hides(self, qapp, cm):
        _mk_cm_window(cm)
        from app.widgets.cards.card_stack_container import CardStackContainer

        stack = CardStackContainer()
        stack.set_container_context("w", ContainerType.LEFT)
        stack.sync_from_manager()
        assert stack.isHidden() is True

    def test_detach_removes(self, qapp, cm):
        _mk_cm_window(cm)
        from PyQt5.QtWidgets import QWidget

        from app.widgets.cards.card_stack_container import CardStackContainer

        stack = CardStackContainer()
        stack.set_container_context("w", ContainerType.LEFT)
        w = QWidget()
        stack.attach_card("c1", w)
        cm._window_data["w"]["dock_visible_cards"][ContainerType.LEFT] = ["c1"]
        stack.sync_from_manager()
        stack.detach_card("c1")
        assert stack.count() == 0
