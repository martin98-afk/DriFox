# -*- coding: utf-8 -*-
"""系统卡关闭后 L2 状态层自动恢复

回归场景：排队消息卡显示中 → 打开模型选择卡（系统卡压制状态层）→
toggle 路径关闭（不经过宿主 _restore_after_system_close）→ 排队卡消失。
机制契约：hide_card 解除系统卡压制时应自动 refresh 受影响的状态层，
恢复不依赖任何调用方"记得调 refresh_layer"。
"""

import pytest

from app.widgets.cards.card_manager import CardManager, ContainerType


class _FakeCard:
    def __init__(self):
        self.visible = False

    def setVisible(self, v):
        self.visible = v

    def windowTitle(self):
        return "fake"


@pytest.fixture()
def cm():
    CardManager.reset_instance()
    yield CardManager.get_instance()
    CardManager.reset_instance()


def _setup(cm, queue_visible_when=lambda: True):
    queue = _FakeCard()
    system = _FakeCard()
    cm.register_card(
        "w",
        ContainerType.BOTTOM,
        "message_queue",
        queue,
        layer="status",
        stackable=True,
        visible_when=queue_visible_when,
    )
    cm.register_card("w", ContainerType.BOTTOM, "model_selector", system, system_card=True)
    return queue, system


class TestSuppressRestore:
    def test_queue_card_restored_after_system_toggle_close(self, cm):
        """toggle 路径关闭系统卡后，被压制的排队卡应自动恢复"""
        _queue, _system = _setup(cm)
        cm.show_card("message_queue", "w")
        assert cm.is_card_visible("message_queue", "w")

        cm.show_card("model_selector", "w")
        assert not cm.is_card_visible("message_queue", "w")

        # toggle_card 关闭 = 纯 hide_card，宿主不会收到 closed 信号去调恢复
        cm.hide_card("model_selector", "w")
        assert cm.is_card_visible("message_queue", "w"), "关闭系统卡后被压制的状态卡应自动恢复"

    def test_queue_card_stays_hidden_when_predicate_false(self, cm):
        """队列已空的排队卡在系统卡关闭后不应被拉出（谓词仍为假）"""
        queue, _system = _setup(cm, queue_visible_when=lambda: False)
        cm.show_card("model_selector", "w")
        cm.hide_card("model_selector", "w")
        assert not cm.is_card_visible("message_queue", "w")
        assert not queue.visible

    def test_system_switch_then_close_restores(self, cm):
        """系统卡互斥切换（开 B 后开 A，B 被互斥隐藏）再关闭 A，状态卡恢复"""
        queue, _a = _setup(cm)
        b = _FakeCard()
        cm.register_card("w", ContainerType.BOTTOM, "settings_b", b, system_card=True)

        cm.show_card("message_queue", "w")
        cm.show_card("settings_b", "w")
        cm.show_card("model_selector", "w")  # 系统卡互斥：settings_b 被隐藏
        assert not cm.is_card_visible("settings_b", "w")
        assert not cm.is_card_visible("message_queue", "w")

        cm.hide_card("model_selector", "w")
        assert cm.is_card_visible("message_queue", "w")
