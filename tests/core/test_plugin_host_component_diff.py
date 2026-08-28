# -*- coding: utf-8 -*-
"""PluginHost 组件差异补载回归测试

背景：watchfile watcher 的 300ms 全局去抖会把同批后到的组件请求合并进
reload_plugin_subsystems，而首拍组件的 rescan_plugin 已消费插件级 changed
→ diff 恒为空（空转）→ 精准型组件（tools/providers/ui）被吞后无人加载，
直到重启（实测：cron-tasks v0.4.0 同批 ui+tools+__manifest__，tools 漏载）。

修复：空转分支探测 manifest 声明 vs 运行时注册差异，精准补载。
本文件覆盖 _detect_unloaded_components / _component_registered 的纯逻辑，
不依赖真实 PluginManager / Qt 事件循环。
"""

import pytest

from app.core.plugin_host_service import PluginHostService
from app.plugins.managers.plugin_manager import PluginInfo


def _make_host() -> PluginHostService:
    """绕过 __init__（QObject/信号初始化）取裸实例——被测方法不依赖实例状态"""
    return PluginHostService.__new__(PluginHostService)


def _make_plugin(name: str, components: dict) -> PluginInfo:
    return PluginInfo(name=name, manifest={"components": components}, path=None)


class _FakePluginManager:
    """最小 PluginManager 替身：list_plugins + is_enabled"""

    def __init__(self, plugins, enabled=None):
        self._plugins = plugins
        self._enabled = enabled

    def list_plugins(self):
        return self._plugins

    def is_enabled(self, name):
        if self._enabled is None:
            return True
        return name in self._enabled


# ================================================================
#  _detect_unloaded_components
# ================================================================


class TestDetectUnloadedComponents:
    def test_reports_missing_components(self, monkeypatch):
        host = _make_host()
        pm = _FakePluginManager(
            [
                _make_plugin("a", {"tools": True, "ui": True}),
                _make_plugin("b", {"tools": True}),
            ]
        )
        # a/tools 已注册，a/ui 未注册；b/tools 探测失败(None 不算漏)
        monkeypatch.setattr(
            PluginHostService,
            "_component_registered",
            lambda self, pname, comp: {"a": {"tools": True, "ui": False}, "b": {}}[pname].get(comp),
        )
        missed = host._detect_unloaded_components(pm)
        assert missed == {"a": ["ui"]}

    def test_skips_disabled_plugins(self, monkeypatch):
        host = _make_host()
        pm = _FakePluginManager([_make_plugin("off", {"tools": True})], enabled=[])
        monkeypatch.setattr(PluginHostService, "_component_registered", lambda self, p, c: False)
        assert host._detect_unloaded_components(pm) == {}

    def test_skips_plugin_without_components(self):
        host = _make_host()
        pm = _FakePluginManager([_make_plugin("bare", {})])
        assert host._detect_unloaded_components(pm) == {}

    def test_none_probe_not_reported(self, monkeypatch):
        """探测返回 None（无法判定）→ 宁漏不误，不进补载清单"""
        host = _make_host()
        pm = _FakePluginManager([_make_plugin("x", {"tools": True, "agents": True})])
        monkeypatch.setattr(PluginHostService, "_component_registered", lambda self, p, c: None)
        assert host._detect_unloaded_components(pm) == {}


# ================================================================
#  _component_registered（tools/providers/ui 三探测分支）
# ================================================================


class _FakeToolRegistryEntry:
    def __init__(self, source):
        self.source = source


class _FakeToolWatcher:
    def __init__(self, loaded, registry_entries):
        self._loaded = loaded
        self._registry = type("R", (), {"list": staticmethod(lambda: registry_entries)})()


class TestComponentRegistered:
    def test_tools_registered_via_loaded_memory(self, monkeypatch):
        import app.plugins.loaders.plugin_tool_loader as ptl

        monkeypatch.setattr(
            ptl, "ensure_plugin_tool_watcher", lambda: _FakeToolWatcher({"p1": {"t1"}}, [])
        )
        host = _make_host()
        assert host._component_registered("p1", "tools") is True

    def test_tools_unregistered_falls_back_to_registry(self, monkeypatch):
        """_loaded 记忆失真（空）时以注册表实际内容兜底"""
        import app.plugins.loaders.plugin_tool_loader as ptl

        entries = [_FakeToolRegistryEntry("plugin:p1"), _FakeToolRegistryEntry("builtin:x")]
        monkeypatch.setattr(ptl, "ensure_plugin_tool_watcher", lambda: _FakeToolWatcher({}, entries))
        host = _make_host()
        assert host._component_registered("p1", "tools") is True
        assert host._component_registered("p2", "tools") is False

    def test_providers_via_registry_sources(self, monkeypatch):
        import app.plugins.loaders.provider_loader as pl

        class _FakeProviderWatcher:
            class _registry:
                provider_sources = staticmethod(lambda: {"plugin:p9"})

        monkeypatch.setattr(pl, "ensure_provider_watcher", lambda: _FakeProviderWatcher())
        host = _make_host()
        assert host._component_registered("p9", "providers") is True
        assert host._component_registered("p8", "providers") is False

    def test_ui_via_is_loaded(self, monkeypatch):
        import app.plugins.registries.ui_plugin_registry as uir

        class _FakeUIReg:
            is_loaded = staticmethod(lambda name: name == "loaded-ui")

        monkeypatch.setattr(uir.UIPluginRegistry, "get_instance", staticmethod(lambda: _FakeUIReg()))
        host = _make_host()
        assert host._component_registered("loaded-ui", "ui") is True
        assert host._component_registered("ghost-ui", "ui") is False

    def test_unknown_component_returns_none(self):
        host = _make_host()
        assert host._component_registered("any", "storages") is None

    def test_watcher_missing_returns_none(self, monkeypatch):
        import app.plugins.loaders.plugin_tool_loader as ptl

        monkeypatch.setattr(ptl, "ensure_plugin_tool_watcher", lambda: None)
        host = _make_host()
        assert host._component_registered("p1", "tools") is None
