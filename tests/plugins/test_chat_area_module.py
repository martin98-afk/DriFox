# -*- coding: utf-8 -*-
"""ChatAreaModule：对话区模块——属性契约 + compose 端到端"""

import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


def test_chat_area_module_contract():
    from app.widgets.modules.chat_area_module import ChatAreaModule

    assert ChatAreaModule.module_id == "chat_area"


def test_compose_builds_chat_area(fresh_registry, qapp):
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    from app.widgets.modules.chat_area_module import ChatAreaModule
    from app.widgets.ui_composition import compose

    fresh_registry.register_ui_module("chat_area", ChatAreaModule, plugin_name="system")

    class _Host(QWidget):
        pass

    host = _Host()

    def _ensure_layout(h):
        if h.layout() is None:
            QVBoxLayout(h)

    # 预置 _top/_bottom_card_container（主程序 setup_ui 头部在 2849-2850 创建）
    host._top_card_container = QWidget(host)
    host._bottom_card_container = QWidget(host)

    report = compose(host, ["chat_area"], root_layout_factory=_ensure_layout)

    assert report["chat_area"] == "system"
    for attr in ("_top_card_container", "_bottom_card_container", "chat_scroll_area", "chat_container", "chat_layout"):
        assert hasattr(host, attr), f"missing host attribute: {attr}"
    assert host.layout() is not None
