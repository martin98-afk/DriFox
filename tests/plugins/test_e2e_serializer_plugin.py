# -*- coding: utf-8 -*-
"""E2E 验收：插件替换消息序列化行为（万物即插件判据）。

覆盖式替换：注册 id="openai" 的自定义 serializer（模拟 user 根覆盖 system 默认）
→ messages_to_api 序列化路径走自定义实现；unregister_source 卸载后回退系统默认。
热重载：serializers reloader 已登记；ensure_serializer_watcher 扫描系统插件目录
注册默认 openai。
"""

import pytest

from app.core import message_content as mc
from app.plugins.contracts.message_serializer import SerializeContext


class _UpperSerializer:
    """自定义 serializer（同 id="openai" 覆盖 system 默认）"""

    id = "openai"

    def serialize_messages(self, messages, ctx: SerializeContext):
        return [{"role": "user", "content": "CUSTOM-OVERRIDE"}]

    def serialize_responses(self, messages, ctx: SerializeContext):
        return ([{"type": "message", "role": "user", "content": []}], "CUSTOM-OVERRIDE")


@pytest.fixture()
def fresh_registry(monkeypatch):
    from app.plugins.registries.serializer_registry import SerializerRegistry

    reg = SerializerRegistry()
    monkeypatch.setattr(SerializerRegistry, "get_instance", staticmethod(lambda: reg))
    return reg


@pytest.fixture()
def fresh_storage_registry(monkeypatch):
    """隔离 StorageRegistry（warmup 冷启动链路不串扰）"""
    from app.plugins.registries.storage_registry import StorageRegistry

    reg = StorageRegistry()
    monkeypatch.setattr(StorageRegistry, "get_instance", staticmethod(lambda: reg))
    return reg


def test_plugin_overrides_default_serializer(fresh_registry, fresh_storage_registry):
    """user 根自定义 serializer 覆盖 system 默认 → messages_to_api 走自定义实现"""
    from app.plugins.loaders.runtime_component_loader import warmup_runtime_components

    warmup_runtime_components()  # 系统默认 openai 注册
    # system 默认先生效
    assert mc.messages_to_api([{"role": "system", "content": "s"}]) == [{"role": "system", "content": "s"}]

    # 插件覆盖（同 id 注册 → 后者覆盖）
    fresh_registry.register(_UpperSerializer(), source="plugin:demo")
    assert mc.messages_to_api([{"role": "user", "content": "hi"}]) == [{"role": "user", "content": "CUSTOM-OVERRIDE"}]
    result = mc.messages_to_responses_input([{"role": "user", "content": "hi"}])
    assert result == ([{"type": "message", "role": "user", "content": []}], "CUSTOM-OVERRIDE")

    # 卸载 → 回退系统默认
    fresh_registry.unregister_source("plugin:demo")
    assert mc.messages_to_api([{"role": "system", "content": "s"}]) == [{"role": "system", "content": "s"}]


def test_serializer_watcher_scans_system_plugin(fresh_registry, fresh_storage_registry):
    """ensure_serializer_watcher → 扫描 plugins/system/serializers/ → 注册默认 openai"""
    from app.plugins.loaders.runtime_component_loader import ensure_serializer_watcher

    watcher = ensure_serializer_watcher()
    assert watcher is not None
    from app.plugins.registries.serializer_registry import SerializerRegistry

    reg = SerializerRegistry.get_instance()
    assert "openai" in reg.serializers()
    # 幂等：二次调用返回同一 watcher
    assert ensure_serializer_watcher() is watcher


def test_builtin_reloaders_cover_serializers():
    """内置 reloader 注册表包含 serializers（热重载分派链）"""
    from app.plugins.builtin_reloaders import _reload_serializers  # noqa: F401
    from app.plugins.builtin_reloaders import RELOADED_COMPONENTS

    assert "serializers" in RELOADED_COMPONENTS


def test_reloader_dispatch_serializers(monkeypatch):
    """reloader 分派链：serializers → runtime loader scan"""
    from app.plugins import kernel

    reg = kernel.ComponentReloaderRegistry()
    calls = []

    def _fake_reloader(ctx):
        calls.append((ctx.component, ctx.plugin_name))
        return True

    reg.register("serializers", _fake_reloader)
    monkeypatch.setattr(kernel, "get_reloader_registry", lambda: reg)

    backend_cls = pytest.importorskip("app.core.backend").ChatBackend
    backend = backend_cls.__new__(backend_cls)
    from unittest.mock import MagicMock

    fake_pm = MagicMock()
    fake_pm.is_initialized.return_value = True
    fake_pm.get_plugin.return_value.components = {"serializers": True}
    fake_pm.get_plugin.return_value.has_component = lambda c: c == "serializers"
    fake_pm.rescan_plugin = MagicMock()
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )
    result = backend._reload_single_plugin("demo-plugin", "serializers")
    assert result["serializers"] is True
    assert ("serializers", "demo-plugin") in calls


def test_kernel_known_components_contains_serializers():
    """serializers 进入 kernel 组件体系（KNOWN_COMPONENTS + plugin_manager 探测）"""
    from app.plugins.kernel import KNOWN_COMPONENTS
    from app.plugins.managers.plugin_manager import _detect_components

    assert "serializers" in KNOWN_COMPONENTS
    (tmp_path := __import__("tempfile").mkdtemp())
    import os

    os.makedirs(os.path.join(tmp_path, "serializers"))
    with open(os.path.join(tmp_path, "serializers", "demo.py"), "w", encoding="utf-8") as f:
        f.write("")
    comps = _detect_components(__import__("pathlib").Path(tmp_path))
    assert comps.get("serializers") is True


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
