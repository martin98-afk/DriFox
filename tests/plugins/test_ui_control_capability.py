# -*- coding: utf-8 -*-
"""workbench:tab 徽标 region 离线单测（不依赖 Qt 环境）。

ui.control 能力已摘除（providers/ui_bridge.py 留档，见其模块 docstring），
本文件保留徽标 region 的契约测试：
1. workbench:tab region：BADGE kind、挂载/覆盖/排序、bus 事件发布
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
