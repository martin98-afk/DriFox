# -*- coding: utf-8 -*-
"""IWindowHost 协议：探测 + 主程序接入 + legacy 鸭子兜底"""

import pytest


class FakeHost:
    """模拟实现 IWindowHost 协议的窗口（带 as_ui_host 入口）"""

    def __init__(self):
        self._window_id = "fake-1"
        self._card_manager = object()

    @property
    def window_id(self):
        return self._window_id

    @property
    def card_manager(self):
        return self._card_manager

    def as_ui_host(self):
        return self


class LegacyDuck:
    """legacy 宿主：仅靠 _card_manager/_window_id 鸭子属性，无 as_ui_host"""

    def __init__(self):
        self._window_id = "legacy-duck"
        self._card_manager = object()


class NotHost:
    pass


def test_is_ui_host():
    from app.plugins.contracts.ui_host import is_ui_host

    assert is_ui_host(FakeHost()) is True
    assert is_ui_host(LegacyDuck()) is False  # 协议入口未实现 → 走 legacy 兜底
    assert is_ui_host(NotHost()) is False
    assert is_ui_host(None) is False


def test_registry_prefers_protocol_entry(monkeypatch):
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    reg = UIPluginRegistry()
    host = FakeHost()
    monkeypatch.setattr(
        "app.widgets.tab_manager_window.TabManagerWindow.get_instance",
        classmethod(lambda cls: host),
    )
    resolved = reg._resolve_global_host()
    assert resolved is host
