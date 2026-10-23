# -*- coding: utf-8 -*-
"""ui.control 能力 + workbench:tab 徽标 region 离线单测（不依赖 Qt 环境）。

覆盖：
1. catalog 声明：ui.control 存在、双 surface
2. 双 surface 注册 + pool 取用 + 动作白名单形状
3. 无 GUI 环境优雅降级（RuntimeError，不崩）
4. workbench:tab region：BADGE kind、挂载/覆盖/排序、bus 事件发布
"""

import pytest

from app.plugins.capabilities import bootstrap
from app.plugins.capabilities.catalog import CATALOG


@pytest.fixture(autouse=True)
def _fresh():
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    yield
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()


def _pool(surface, declared):
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered(surface)
    return CapabilityRegistry.get_instance().build_pool(surface=surface, host=object(), plugin="demo", declared=declared)


# ── catalog 声明 ──


def test_catalog_declares_ui_control_both_surfaces():
    spec = CATALOG["ui.control"]
    assert spec.surfaces == frozenset({"ui", "tool"})


def test_ui_control_registered_on_both_surfaces():
    from app.plugins.capabilities.registry import CapabilityRegistry

    for surface in ("ui", "tool"):
        CapabilityRegistry.reset_instance()
        bootstrap.reset_bootstrap()
        _pool(surface, {"ui.control"})
        assert CapabilityRegistry.get_instance().has("ui.control", surface=surface)


def test_ui_control_action_whitelist_shape():
    pool = _pool("tool", {"ui.control"})
    actions = pool["ui.control"]
    assert set(actions.keys()) == {"snapshot", "switch_tab", "run_command", "notify", "set_input"}
    assert all(callable(a) for a in actions.values())


def test_ui_control_no_gui_raises_runtime_error(monkeypatch):
    """headless / 测试环境无 QApplication → 动作抛 RuntimeError（可降级，不崩）"""
    from PyQt5.QtWidgets import QApplication

    monkeypatch.setattr(QApplication, "instance", staticmethod(lambda: None))
    import app.plugins.capabilities.providers.ui_bridge as bridge

    monkeypatch.setattr(bridge, "_DISPATCHER", None)
    pool = _pool("tool", {"ui.control"})
    with pytest.raises(RuntimeError, match="无 GUI 环境"):
        pool["ui.control"]["snapshot"]()


def test_ui_control_snapshot_fields_shape(monkeypatch):
    """主线程直调路径：动作在无窗口时返回 has_ui=False 的全 None 快照"""
    import app.plugins.capabilities.providers.ui_bridge as bridge

    monkeypatch.setattr(bridge, "_resolve_widget", lambda: None)
    monkeypatch.setattr(bridge, "_resolve_tab_window", lambda: None)
    snap = bridge._action_snapshot()
    assert snap["has_ui"] is False
    assert snap["input_text"] is None and snap["workbench_tab"] is None


# ── workbench:tab 徽标 region ──


def test_badge_region_declared():
    from app.plugins.contracts.ui_slots import BADGE, VALID_REGION_KINDS

    assert BADGE in VALID_REGION_KINDS
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    registry = UIPluginRegistry.get_instance()
    region = registry._regions.get("workbench:tab")
    assert region is not None and region["kind"] == BADGE


def test_badge_slot_entry_mount_and_priority():
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    registry = UIPluginRegistry.get_instance()
    registry.register_slot_entry(
        "workbench:tab", "b1", "plug-a", priority=0, payload="3", metadata={"tab_id": "git-panel"}
    )
    registry.register_slot_entry(
        "workbench:tab", "b2", "plug-b", priority=10, payload="9", metadata={"tab_id": "git-panel"}
    )
    entries = registry.get_region_entries("workbench:tab")
    assert [e.entry_id for e in entries] == ["b2", "b1"], "priority 降序"
    # 同 tab 取 priority 最高者（消费方约定）
    top = entries[0]
    assert top.payload == "9" and top.metadata["tab_id"] == "git-panel"


def test_badge_mount_publishes_bus_event():
    """挂载徽标条目 → 发布 workbench.badges_changed（WorkbenchPanel 消费）"""
    from app.core.infra.ui_event_bus import EV_WORKBENCH_BADGES_CHANGED, UIEventBus
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    seen = []

    def _cb(payload):
        seen.append(payload)

    bus = UIEventBus.get_instance()
    bus.subscribe(EV_WORKBENCH_BADGES_CHANGED, _cb)
    registry = UIPluginRegistry.get_instance()
    registry.register_slot_entry(
        "workbench:tab", "bx", "plug-x", payload="N", metadata={"tab_id": "some-page"}
    )
    assert len(seen) == 1
    # 非 badge 区域不触发
    registry.register_slot_entry("sidebar", "sx", "plug-x", payload=object())
    assert len(seen) == 1
    bus.unsubscribe(EV_WORKBENCH_BADGES_CHANGED, _cb)
