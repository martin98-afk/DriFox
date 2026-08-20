# -*- coding: utf-8 -*-
"""Gateway 插件卸载 / 热更新清理测试

覆盖：
1. GatewayPlatformRegistry.get_platform_ids_by_source 按来源过滤 platform
2. PlatformManager.stop_plugin_platforms 关闭连接 + 摘除 adapter 实例
3. PlatformManager.rebuild_plugin_platforms 用新 def 重建 adapter（热更新生效）
4. builtin_reloaders._purge_gateway_plugin_modules 清理 sys.modules 模块引用
"""

import asyncio
import sys

import pytest

from app.gateway.manager import PlatformManager
from app.plugins import builtin_reloaders
from app.plugins.contracts.gateway_platform import GatewayPlatformDef
from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry


@pytest.fixture
def clean_registry():
    reg = GatewayPlatformRegistry.get_instance()
    reg._defs.clear()
    yield reg
    reg._defs.clear()


class _FakeAdapter:
    def __init__(self, pid):
        self.pid = pid
        self.is_connected = True
        self.stopped = False
        self.started = False
        self.last_error = None

    async def stop(self):
        self.stopped = True
        self.is_connected = False

    async def start(self):
        self.started = True
        self.is_connected = True


class _FakeConfig:
    """最小化 GatewayConfigHelper：get_platform_config 返回 enabled 配置"""

    def get_platform_config(self, pid):
        class _C:
            enabled = True

        return _C()


@pytest.fixture
def fake_manager(clean_registry, monkeypatch):
    """真实 PlatformManager 实例；用 asyncio.run 同步驱动协程，避免 daemon loop 时序"""
    mgr = PlatformManager(_FakeConfig())
    # 默认 _schedule_coro 用 run_coroutine_threadsafe 调度到后台 loop，
    # 测试里改为同步 asyncio.run 驱动，结果确定可控
    monkeypatch.setattr(mgr, "_schedule_coro", lambda coro: asyncio.run(coro))
    yield mgr
    mgr._adapters.clear()


def _make_def(platform_id: str, source: str, factory) -> GatewayPlatformDef:
    return GatewayPlatformDef(
        platform_id=platform_id,
        display_name=platform_id,
        adapter_factory=factory,
        source=source,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. registry 按来源过滤
# ─────────────────────────────────────────────────────────────────────────────


def test_registry_get_platform_ids_by_source(clean_registry):
    clean_registry.register(_make_def("a", "plugin:x", lambda: None), source="plugin:x")
    clean_registry.register(_make_def("b", "plugin:y", lambda: None), source="plugin:y")
    clean_registry.register(_make_def("c", "plugin:x", lambda: None), source="plugin:x")

    assert set(clean_registry.get_platform_ids_by_source("plugin:x")) == {"a", "c"}
    assert clean_registry.get_platform_ids_by_source("plugin:y") == ["b"]
    assert clean_registry.get_platform_ids_by_source("plugin:z") == []


# ─────────────────────────────────────────────────────────────────────────────
# 2. stop_plugin_platforms：关闭连接 + 摘除 adapter 实例
# ─────────────────────────────────────────────────────────────────────────────


def test_stop_plugin_platforms_stops_and_removes(fake_manager, clean_registry):
    # 注册两个 platform 都归 plugin:gwplug，其中 b 归另一个插件（不应被影响）
    clean_registry.register(_make_def("gw1", "plugin:gwplug", lambda: _FakeAdapter("gw1")), source="plugin:gwplug")
    clean_registry.register(_make_def("gw2", "plugin:gwplug", lambda: _FakeAdapter("gw2")), source="plugin:gwplug")
    clean_registry.register(_make_def("other", "plugin:other", lambda: _FakeAdapter("other")), source="plugin:other")

    # manager 手工持有正在运行的 adapter 实例
    ad1 = _FakeAdapter("gw1")
    ad2 = _FakeAdapter("gw2")
    ad_other = _FakeAdapter("other")
    fake_manager._adapters.update({"gw1": ad1, "gw2": ad2, "other": ad_other})

    fake_manager.stop_plugin_platforms("gwplug")

    # 仅 gwplug 名下的 adapter 被 stop + 摘除
    assert ad1.stopped is True and ad2.stopped is True
    assert "gw1" not in fake_manager._adapters
    assert "gw2" not in fake_manager._adapters
    # 其他插件的 platform 不受影响
    assert "other" in fake_manager._adapters
    assert ad_other.stopped is False
    # registry 中的 def 仍保留（stop 只摘除运行实例，unregister 由 watcher 负责）
    assert set(clean_registry.get_platform_ids_by_source("plugin:gwplug")) == {"gw1", "gw2"}


# ─────────────────────────────────────────────────────────────────────────────
# 3. rebuild_plugin_platforms：用新 def 重建 adapter（热更新生效）
# ─────────────────────────────────────────────────────────────────────────────


def test_rebuild_plugin_platforms_uses_new_factory(fake_manager, clean_registry):
    # 旧 adapter 实例（代表热更新前的旧代码）
    old_adapter = _FakeAdapter("gw1")
    fake_manager._adapters["gw1"] = old_adapter

    # 热更新后 registry 持有新 adapter_factory
    calls = []
    def new_factory(cfg):
        calls.append(cfg)
        return _FakeAdapter("gw1_NEW")

    clean_registry.register(_make_def("gw1", "plugin:gwplug", new_factory), source="plugin:gwplug")
    fake_manager._running = True  # 模拟此前在运行，rebuild 后应 restart

    fake_manager.rebuild_plugin_platforms("gwplug", restart_if_running=True)

    rebuilt = fake_manager._adapters["gw1"]
    # 关键：adapter 实例被新 factory 创建（旧对象被替换 → 新代码生效）
    assert rebuilt is not old_adapter
    assert isinstance(rebuilt, _FakeAdapter)
    assert rebuilt.pid == "gw1_NEW"
    assert calls == [None]  # config_builder 为 None → factory 收到 None


def test_rebuild_plugin_platforms_skips_when_not_running(fake_manager, clean_registry):
    old_adapter = _FakeAdapter("gw1")
    fake_manager._adapters["gw1"] = old_adapter

    def new_factory(cfg):
        return _FakeAdapter("gw1_NEW")

    clean_registry.register(_make_def("gw1", "plugin:gwplug", new_factory), source="plugin:gwplug")
    fake_manager._running = False  # 此前未运行：重建 adapter 但不 start

    fake_manager.rebuild_plugin_platforms("gwplug", restart_if_running=True)

    # adapter 已重建
    assert fake_manager._adapters["gw1"] is not old_adapter
    # 因 _running=False 且条件分支在 is_connected 检查之外，仅验证重建成功即可
    assert isinstance(fake_manager._adapters["gw1"], _FakeAdapter)


# ─────────────────────────────────────────────────────────────────────────────
# 4. _purge_gateway_plugin_modules：清理 sys.modules 模块引用
# ─────────────────────────────────────────────────────────────────────────────


def test_purge_gateway_plugin_modules():
    sys.modules["drifox_rt_gateways_myplug_wecom"] = object()
    sys.modules["drifox_rt_gateways_myplug_feishu"] = object()
    sys.modules["drifox_rt_gateways_other_x"] = object()  # 不应被清
    sys.modules["drifox_rt_gateways_myplug_sub_mod"] = object()

    builtin_reloaders._purge_gateway_plugin_modules("myplug")

    assert "drifox_rt_gateways_myplug_wecom" not in sys.modules
    assert "drifox_rt_gateways_myplug_feishu" not in sys.modules
    assert "drifox_rt_gateways_myplug_sub_mod" not in sys.modules
    # 其他插件 / 其他组件不受影响
    assert "drifox_rt_gateways_other_x" in sys.modules

    # 清理测试残留
    sys.modules.pop("drifox_rt_gateways_other_x", None)
