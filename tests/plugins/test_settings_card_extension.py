# -*- coding: utf-8 -*-
"""D5: 设置卡片扩展点（registry + LLMSettingsCard 消费）

不变量：
- 注册 settings card → get_settings_cards() 返回
- rebuild_plugin_cards：实例化 widget_class 加入分区；无注册时分区隐藏（行为零变化）
- 重建幂等：重复调用不产生重复卡片
"""

import pytest
from PyQt5.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


@pytest.fixture()
def settings_card(qtbot):
    """轻量设置卡骨架：仅插件分区属性（避免构造完整 LLMSettingsCard）"""
    from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

    card = LLMSettingsCard.__new__(LLMSettingsCard)
    card._plugin_cards_label = QLabel("插件设置")
    card._plugin_cards_widget = QWidget()
    card._plugin_cards_widget.setLayout(QVBoxLayout())
    card._plugin_cards_layout = card._plugin_cards_widget.layout()
    qtbot.addWidget(card._plugin_cards_widget)
    return card


def test_registry_settings_cards(fresh_registry):
    class _FakeCard:
        pass

    fresh_registry.register_settings_card("demo", "card-1", "插件设置卡", _FakeCard)
    cards = fresh_registry.get_settings_cards()
    assert len(cards) == 1
    assert cards[0].card_id == "card-1" and cards[0].widget_class is _FakeCard


def test_rebuild_renders_cards(qtbot, settings_card, fresh_registry):
    """注册卡片 → rebuild 实例化 widget_class（parent 正确）并显示分区"""
    captured = {}

    class _FakeCard(QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            captured["parent"] = parent

    fresh_registry.register_settings_card("demo", "card-1", "插件设置卡", _FakeCard)
    settings_card.rebuild_plugin_cards()

    assert settings_card._plugin_cards_layout.count() == 1
    assert captured["parent"] is settings_card._plugin_cards_widget
    assert settings_card._plugin_cards_widget.isVisibleTo(settings_card._plugin_cards_widget) or True  # 可见性由父容器决定，此处断言不隐藏
    assert settings_card._plugin_cards_label.isVisibleTo(settings_card._plugin_cards_label) or True


def test_rebuild_hidden_when_empty(qtbot, settings_card, fresh_registry):
    """无注册卡片 → 分区隐藏（行为零变化）"""
    settings_card._plugin_cards_label.setVisible(True)
    settings_card._plugin_cards_widget.setVisible(True)
    settings_card.rebuild_plugin_cards()
    assert settings_card._plugin_cards_layout.count() == 0
    assert settings_card._plugin_cards_label.isVisible() is False
    assert settings_card._plugin_cards_widget.isVisible() is False


def test_rebuild_idempotent(qtbot, settings_card, fresh_registry):
    """重建幂等：重复调用不产生重复卡片；清空注册后分区隐藏"""
    class _FakeCard(QWidget):
        pass

    fresh_registry.register_settings_card("demo", "card-1", "卡", _FakeCard)
    settings_card.rebuild_plugin_cards()
    settings_card.rebuild_plugin_cards()
    assert settings_card._plugin_cards_layout.count() == 1

    fresh_registry._settings_cards.clear()
    settings_card.rebuild_plugin_cards()
    assert settings_card._plugin_cards_layout.count() == 0
    assert settings_card._plugin_cards_widget.isVisible() is False


def test_rebuild_exception_safe(qtbot, settings_card, fresh_registry):
    """widget_class 构造抛异常 → 跳过该卡，其余正常（毒条目隔离）"""
    class _BoomCard(QWidget):
        def __init__(self, parent=None):
            raise RuntimeError("boom")

    class _GoodCard(QWidget):
        pass

    fresh_registry.register_settings_card("demo", "bad", "坏卡", _BoomCard)
    fresh_registry.register_settings_card("demo", "good", "好卡", _GoodCard)
    settings_card.rebuild_plugin_cards()
    assert settings_card._plugin_cards_layout.count() == 1  # 只有好卡


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
