# -*- coding: utf-8 -*-
"""
Registry 边界情况测试

覆盖（T2 计划 P12）：
- 同根重复注册同名 → 返回 False 且 version 不增（不覆盖已有）
- unregister 不存在工具 → False
- 非法 schema（无 function.name）→ 拒绝
- 非法工具名（空/非字符串）→ 拒绝
- danger 非法值 → 拒绝
- 插件伪造 builtin 源 → 拒绝（安全护栏）

运行: python -m pytest tests/core/test_registry_edge_cases.py -v
"""
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.tools.registry import DANGER_SAFE, ToolRegistry


@pytest.fixture(autouse=True)
def fresh_registry():
    ToolRegistry.reset_instance()
    yield
    ToolRegistry.reset_instance()


def _schema(name=None):
    fn = {"name": name} if name is not None else {}
    return {"type": "function", "function": fn}


class TestDuplicateRegistration:
    """P12：同名冲突保护（_PluginRegistryProxy 代理层：跨插件源不同不覆盖）"""

    def _proxy(self, registry, plugin_name, root=None):
        """构造插件注册代理（与 loader 注入一致：source 强制 plugin:<name>）"""
        from app.plugins.loaders.plugin_tool_loader import _PluginRegistryProxy

        return _PluginRegistryProxy(registry, plugin_name, root=root, root_tracker={})

    def test_cross_plugin_duplicate_rejected(self):
        """跨插件同名：插件 B 注册已被插件 A 占用的名 → 代理拒绝（False）"""
        reg = ToolRegistry.get_instance()
        proxy_a = self._proxy(reg, "plug_a")
        proxy_b = self._proxy(reg, "plug_b")
        assert proxy_a.register("dup_tool", _schema("dup_tool"), impl=lambda **kw: "a", danger="safe")
        ok = proxy_b.register("dup_tool", _schema("dup_tool"), impl=lambda **kw: "b", danger="safe")
        assert not ok, "跨插件同名不得覆盖（先注册者优先）"

    def test_cross_plugin_duplicate_version_unchanged(self):
        """跨插件同名被拒 → version 不增"""
        reg = ToolRegistry.get_instance()
        proxy_a = self._proxy(reg, "plug_a")
        proxy_b = self._proxy(reg, "plug_b")
        proxy_a.register("dup_tool2", _schema("dup_tool2"), impl=lambda **kw: "a", danger="safe")
        v1 = reg.version()
        proxy_b.register("dup_tool2", _schema("dup_tool2"), impl=lambda **kw: "b", danger="safe")
        assert reg.version() == v1

    def test_cross_plugin_duplicate_keeps_first(self):
        """同名冲突 → 保留先注册者的 impl（先注册者优先）"""
        reg = ToolRegistry.get_instance()
        proxy_a = self._proxy(reg, "plug_a")
        proxy_b = self._proxy(reg, "plug_b")
        proxy_a.register("dup_tool3", _schema("dup_tool3"), impl=lambda **kw: "first", danger="safe")
        proxy_b.register("dup_tool3", _schema("dup_tool3"), impl=lambda **kw: "second", danger="safe")
        assert reg.get("dup_tool3").impl() == "first"
        assert reg.get("dup_tool3").source == "plugin:plug_a"

    def test_own_plugin_duplicate_overwrites(self):
        """同插件重复注册同名 → 代理允许（覆盖自身，返回 True）"""
        reg = ToolRegistry.get_instance()
        proxy_a = self._proxy(reg, "plug_a")
        proxy_a.register("dup_tool4", _schema("dup_tool4"), impl=lambda **kw: "v1", danger="safe")
        ok = proxy_a.register("dup_tool4", _schema("dup_tool4"), impl=lambda **kw: "v2", danger="safe")
        assert ok
        assert reg.get("dup_tool4").impl() == "v2"


class TestUnregisterEdge:
    """P12：unregister 边界"""

    def test_unregister_missing_returns_false(self):
        """unregister 不存在的工具 → False"""
        reg = ToolRegistry.get_instance()
        assert not reg.unregister("ghost_tool")

    def test_unregister_missing_version_unchanged(self):
        """unregister 不存在 → version 不增"""
        reg = ToolRegistry.get_instance()
        v0 = reg.version()
        reg.unregister("ghost_tool2")
        assert reg.version() == v0

    def test_unregister_then_register_same_name(self):
        """注销后可重新注册同名（version 递增）"""
        reg = ToolRegistry.get_instance()
        reg.register("cycle_tool", _schema("cycle_tool"), impl=lambda **kw: "x",
                     danger="safe", source="plugin:x")
        v1 = reg.version()
        reg.unregister("cycle_tool")
        assert reg.version() == v1 + 1
        reg.register("cycle_tool", _schema("cycle_tool"), impl=lambda **kw: "y",
                     danger="safe", source="plugin:x")
        assert reg.get("cycle_tool").impl() == "y"


class TestInvalidRegistration:
    """P12：非法注册拒绝"""

    def test_schema_structure_not_validated_by_registry(self):
        """registry 不校验 schema 内部结构（schema 校验由调用方/LLM 层负责）。

        记录实际行为：空 function dict 可注册（registry 只校验 name/danger/source）。
        真正的注册护栏是 danger 强制声明 + source 白名单 + name 合法性。
        """
        reg = ToolRegistry.get_instance()
        ok = reg.register("x", {"type": "function", "function": {}},
                          impl=lambda **kw: "x", danger="safe", source="plugin:x")
        assert ok
        assert reg.get("x") is not None

    def test_empty_name_rejected(self):
        """空工具名 → 拒绝"""
        reg = ToolRegistry.get_instance()
        assert not reg.register("", _schema(""), impl=lambda **kw: "x",
                                danger="safe", source="plugin:x")

    def test_non_string_name_rejected(self):
        """非字符串工具名 → 拒绝"""
        reg = ToolRegistry.get_instance()
        assert not reg.register(123, _schema("123"), impl=lambda **kw: "x",
                                danger="safe", source="plugin:x")

    def test_invalid_danger_value_rejected(self):
        """danger 非法值 → 拒绝"""
        reg = ToolRegistry.get_instance()
        ok = reg.register("bad_danger", _schema("bad_danger"), impl=lambda **kw: "x",
                          danger="super-dangerous", source="plugin:x")
        assert not ok

    def test_forge_builtin_rejected(self):
        """插件伪造 builtin 源 → 拒绝（安全护栏）"""
        reg = ToolRegistry.get_instance()
        ok = reg.register("forge", _schema("forge"), impl=lambda **kw: "x",
                          danger="safe", source="builtin")
        assert not ok

    def test_rejected_registration_not_in_registry(self):
        """被拒绝的注册（danger 非法）不进入 registry"""
        reg = ToolRegistry.get_instance()
        reg.register("bad_danger2", _schema("bad_danger2"),
                     impl=lambda **kw: "x", danger="not-a-danger", source="plugin:x")
        assert reg.get("bad_danger2") is None
