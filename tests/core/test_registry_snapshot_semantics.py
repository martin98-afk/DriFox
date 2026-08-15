# -*- coding: utf-8 -*-
"""
Registry 快照语义测试（热重载并发安全）

覆盖（T2 计划 P9）：
- snapshot() 持有 → unregister → 旧快照仍可调 impl（执行中调用不受热重载影响）
- version 单调递增：register/unregister/clear 每次变更 +1；无变更不增
- snapshot 是独立 dict：快照修改不影响 registry

运行: python -m pytest tests/core/test_registry_snapshot_semantics.py -v
"""
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def fresh_registry():
    ToolRegistry.reset_instance()
    yield
    ToolRegistry.reset_instance()


def _schema(name):
    return {"type": "function", "function": {"name": name}}


class TestSnapshotSemantics:
    """P9：snapshot 持有后 unregister 不影响已持有引用"""

    def test_snapshot_holds_impl_after_unregister(self):
        """snapshot() → unregister → 快照中旧注册仍可调 impl"""
        reg = ToolRegistry.get_instance()

        def impl(**kw):
            return "snapshot-result"

        reg.register("snap_tool", _schema("snap_tool"), impl=impl, danger="safe", source="plugin:x")
        snap = reg.snapshot()
        assert "snap_tool" in snap

        # 热重载：注销工具
        assert reg.unregister("snap_tool")
        assert reg.get("snap_tool") is None

        # 已持有快照仍可执行（执行中调用不中断）
        old = snap["snap_tool"]
        assert old.impl() == "snapshot-result"
        assert old.name == "snap_tool"

    def test_snapshot_holds_after_clear(self):
        """clear() 后快照中的旧注册仍可用"""
        reg = ToolRegistry.get_instance()
        reg.register("snap2", _schema("snap2"), impl=lambda **kw: "x", danger="safe", source="plugin:x")
        snap = reg.snapshot()
        reg.clear()
        assert reg.names() == []
        assert snap["snap2"].impl() == "x"

    def test_snapshot_is_independent_dict(self):
        """快照是独立 dict：修改快照不影响 registry"""
        reg = ToolRegistry.get_instance()
        reg.register("snap3", _schema("snap3"), impl=lambda **kw: "x", danger="safe", source="plugin:x")
        snap = reg.snapshot()
        snap.pop("snap3")  # 修改快照
        assert reg.get("snap3") is not None  # registry 不受影响


class TestVersionMonotonic:
    """P9：version 单调递增"""

    def test_register_increments_version(self):
        reg = ToolRegistry.get_instance()
        v0 = reg.version()
        reg.register("v_tool", _schema("v_tool"), impl=lambda **kw: "x", danger="safe", source="plugin:x")
        assert reg.version() == v0 + 1

    def test_unregister_increments_version(self):
        reg = ToolRegistry.get_instance()
        reg.register("v_tool2", _schema("v_tool2"), impl=lambda **kw: "x", danger="safe", source="plugin:x")
        v1 = reg.version()
        reg.unregister("v_tool2")
        assert reg.version() == v1 + 1

    def test_clear_increments_version(self):
        reg = ToolRegistry.get_instance()
        reg.register("v_tool3", _schema("v_tool3"), impl=lambda **kw: "x", danger="safe", source="plugin:x")
        v1 = reg.version()
        reg.clear()
        assert reg.version() == v1 + 1

    def test_no_change_no_increment(self):
        """无变更（重复 unregister/clear 空）→ version 不增"""
        reg = ToolRegistry.get_instance()
        v0 = reg.version()
        assert not reg.unregister("not_exist")
        assert reg.version() == v0
        reg.clear()  # 空 clear 无变更
        assert reg.version() == v0

    def test_reject_register_no_increment(self):
        """被拒绝的注册（非法）→ version 不增"""
        reg = ToolRegistry.get_instance()
        v0 = reg.version()
        # 插件工具未声明 danger → 拒绝
        reg.register("bad_tool", _schema("bad_tool"), impl=lambda **kw: "x", source="plugin:x")
        assert reg.version() == v0
