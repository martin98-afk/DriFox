# -*- coding: utf-8 -*-
"""消息卡内嵌面板槽位（UIPluginRegistry + MessageCard）

不变量：
- 注册/查询/排序（order 升序）
- attach 把面板挂到宿主最新 assistant 卡（卡片侧 ensure_inline_panel 被调用）
- 跨卡迁移：旧卡 remove_inline_panel 被调用，新卡接管
- detach 摘除当前卡实例，不改注册
- unload_plugin 清理该插件的面板注册与实例
"""

import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


class _StubCard:
    """MessageCard 最小替身：只实现槽位消费方需要的两个方法。"""

    def __init__(self, name: str):
        self.name = name
        self.ensured = []
        self.removed = []

    def ensure_inline_panel(self, panel_id):
        self.ensured.append(panel_id)
        return f"panel@{self.name}"

    def remove_inline_panel(self, panel_id):
        self.removed.append(panel_id)


class _StubHost:
    """MainWidget 最小替身：_current_assistant_card + 最新卡查找。"""

    def __init__(self, card=None, cards=None):
        self._current_assistant_card = card
        self._cards = list(cards or [])

    def _find_latest_assistant_card(self):
        return self._cards[-1] if self._cards else None


class _FakePanel:
    pass


def test_register_and_query(fresh_registry):
    fresh_registry.register_card_inline_panel("demo", "panel-b", _FakePanel, order=200)
    fresh_registry.register_card_inline_panel("demo", "panel-a", _FakePanel, order=100)

    assert fresh_registry.get_card_inline_panel("panel-a").plugin_name == "demo"
    assert fresh_registry.get_card_inline_panel("missing") is None
    # order 升序
    assert [i.panel_id for i in fresh_registry.get_card_inline_panels()] == ["panel-a", "panel-b"]


def test_attach_to_latest_card(fresh_registry):
    fresh_registry.register_card_inline_panel("demo", "panel", _FakePanel)
    card = _StubCard("latest")
    host = _StubHost(cards=[card])

    panel = fresh_registry.attach_card_inline_panel("panel", main_widget=host)

    assert panel == "panel@latest"
    assert card.ensured == ["panel"]


def test_attach_prefers_current_assistant_card(fresh_registry):
    fresh_registry.register_card_inline_panel("demo", "panel", _FakePanel)
    current, latest = _StubCard("current"), _StubCard("latest")
    host = _StubHost(card=current, cards=[latest])

    assert fresh_registry.attach_card_inline_panel("panel", main_widget=host) == "panel@current"
    assert current.ensured == ["panel"]
    assert latest.ensured == []


def test_attach_migrates_off_previous_card(fresh_registry):
    fresh_registry.register_card_inline_panel("demo", "panel", _FakePanel)
    first, second = _StubCard("first"), _StubCard("second")

    host = _StubHost(card=first)
    fresh_registry.attach_card_inline_panel("panel", main_widget=host)
    # 宿主切到新卡后再 attach
    host._current_assistant_card = second
    fresh_registry.attach_card_inline_panel("panel", main_widget=host)

    assert first.removed == ["panel"]
    assert second.ensured == ["panel"]


def test_attach_without_card_returns_none(fresh_registry):
    fresh_registry.register_card_inline_panel("demo", "panel", _FakePanel)
    assert fresh_registry.attach_card_inline_panel("panel", main_widget=_StubHost()) is None


def test_attach_unknown_panel_returns_none(fresh_registry):
    assert fresh_registry.attach_card_inline_panel("missing", main_widget=_StubHost()) is None


def test_detach_removes_instance_but_keeps_registration(fresh_registry):
    fresh_registry.register_card_inline_panel("demo", "panel", _FakePanel)
    card = _StubCard("card")
    host = _StubHost(card=card)
    fresh_registry.attach_card_inline_panel("panel", main_widget=host)

    fresh_registry.detach_card_inline_panel("panel")

    assert card.removed == ["panel"]
    assert fresh_registry.get_card_inline_panel("panel") is not None


def test_unload_plugin_cleans_inline_panels(fresh_registry):
    fresh_registry.register_card_inline_panel("demo", "panel", _FakePanel)
    card = _StubCard("card")
    fresh_registry.attach_card_inline_panel("panel", main_widget=_StubHost(card=card))
    # unload 的幂等判定要求插件在 loaded 集合内
    fresh_registry._loaded_plugins.add("demo")

    fresh_registry.unload_plugin("demo")

    assert card.removed == ["panel"]
    assert fresh_registry.get_card_inline_panel("panel") is None


def test_inline_panel_does_not_touch_floating_cards(fresh_registry):
    """内嵌面板与浮动卡是两套注册表，互不影响。"""
    fresh_registry.register_card_inline_panel("demo", "panel", _FakePanel)
    assert fresh_registry.get_floating_cards() == {}
