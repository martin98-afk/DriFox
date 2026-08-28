# -*- coding: utf-8 -*-
"""register_floating_card stack 声明：metadata 透传到 widget 实例属性"""


import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


def test_stack_metadata_sets_widget_property(fresh_registry, qapp, monkeypatch):
    from PySide6.QtWidgets import QWidget

    class _StackableCard(QWidget):
        pass

    # 隔离命令注册（避免依赖 CommandManager）
    monkeypatch.setattr(fresh_registry, "_register_command_for_card", lambda info: None)

    fresh_registry.register_floating_card(
        "demo", "st1", _StackableCard, container="left", title="堆叠卡", metadata={"stack": True}
    )
    fresh_registry.register_floating_card(
        "demo", "norm1", QWidget, container="left", title="普通卡"
    )
    assert fresh_registry.get_floating_cards()["st1"].metadata.get("stack") is True
    assert fresh_registry.get_floating_cards()["norm1"].metadata.get("stack") is None
