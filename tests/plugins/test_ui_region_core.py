# -*- coding: utf-8 -*-
"""UIRegion 通用挂载模型：declare_region / register_slot_entry / get_region_entries
+ priority 覆盖 + 未声明区域拒绝 + unload 清理"""

import pytest

from app.plugins.contracts.ui_slots import MENU, SlotEntry
from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


class TestRegionCore:
    def test_declare_and_register(self, fresh_registry):
        fresh_registry.declare_region("menu:input_area", MENU, "输入框右键菜单")
        fresh_registry.register_slot_entry(
            "menu:input_area", "paste-enhance", "demo", priority=0, payload={"label": "粘贴增强"}
        )
        entries = fresh_registry.get_region_entries("menu:input_area")
        assert len(entries) == 1
        e = entries[0]
        assert isinstance(e, SlotEntry)
        assert e.entry_id == "paste-enhance" and e.plugin_name == "demo"
        assert e.payload == {"label": "粘贴增强"}

    def test_undeclared_region_register_raises(self, fresh_registry):
        with pytest.raises(ValueError, match="undeclared region"):
            fresh_registry.register_slot_entry("ghost", "x", "demo")

    def test_priority_override(self, fresh_registry):
        fresh_registry.declare_region("sidebar", "list_item")
        fresh_registry.register_slot_entry("sidebar", "dup", "demo", priority=1, payload="低")
        fresh_registry.register_slot_entry("sidebar", "dup", "demo", priority=9, payload="高")
        fresh_registry.register_slot_entry("sidebar", "dup", "demo", priority=2, payload="再低")
        entries = fresh_registry.get_region_entries("sidebar")
        assert len(entries) == 1 and entries[0].payload == "高"

    def test_get_region_entry_direct(self, fresh_registry):
        fresh_registry.declare_region("sidebar", "list_item")
        fresh_registry.register_slot_entry("sidebar", "only", "demo", payload=42)
        assert fresh_registry.get_region_entry("sidebar", "only").payload == 42

    def test_unload_plugin_clears_entries(self, fresh_registry):
        fresh_registry.declare_region("sidebar", "list_item")
        fresh_registry.register_slot_entry("sidebar", "x", "demo")
        fresh_registry.unload_plugin("demo")
        assert fresh_registry.get_region_entries("sidebar") == []

    def test_declare_region_idempotent(self, fresh_registry):
        fresh_registry.declare_region("sidebar", "list_item", "侧边栏")
        fresh_registry.declare_region("sidebar", "list_item", "侧边栏2")  # 重复声明不抛
        fresh_registry.register_slot_entry("sidebar", "x", "demo")
        assert len(fresh_registry.get_region_entries("sidebar")) == 1
