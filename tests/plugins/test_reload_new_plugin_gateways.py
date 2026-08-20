# -*- coding: utf-8 -*-
"""新插件 __NEW__ 增量加载路径的 gateways 分派回归测试

背景（2026-08-20 bug）：卸载后重装 gateway 插件（watcher 判定全新安装走
__NEW__ → _reload_new_plugin）后机器人无响应。根因：_reload_new_plugin
第 8 步硬编码 ("tools", "providers", "team_templates") 三组件，gateways
不在分派集合 → def 不注册 / adapter 不建 / 连接不启。

修复：第 8 步按 kernel.COMPONENT_ORDER 遍历（排除 1-7 手工处理组件），
gateways/model_adapters/loop_policies/storages/serializers 全覆盖。
"""

from unittest.mock import MagicMock

import pytest


class _FakePlugin:
    def __init__(self, components: dict):
        self.components = components
        self.path = "/fake/plugin"

    def has_component(self, c: str) -> bool:
        return bool(self.components.get(c))


def _build_backend():
    """最小 ChatBackend 实例（不跑 __init__ 全链）"""
    from app.core.backend import ChatBackend

    backend = ChatBackend.__new__(ChatBackend)
    backend._agent_manager = None
    return backend


def _install_pm(monkeypatch, components: dict, plugin_name: str):
    pm = MagicMock()
    pm.is_initialized.return_value = True
    pm.get_plugin.return_value = _FakePlugin(components)
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        lambda: pm,
        raising=True,
    )
    # _reload_new_plugin 内部 import 后调用：PluginManager.get_instance()
    return pm


@pytest.fixture()
def gateway_reload_spies(monkeypatch):
    """替换 _reload_gateways 依赖的两个平台句柄为 spy，捕获调用"""
    mgr = MagicMock()
    monkeypatch.setattr("app.gateway.manager.get_platform_manager", lambda: mgr)

    watcher = MagicMock()
    monkeypatch.setattr(
        "app.plugins.loaders.runtime_component_loader.ensure_gateway_watcher",
        lambda: watcher,
    )
    return mgr, watcher


def test_reload_new_plugin_dispatches_gateways(monkeypatch, gateway_reload_spies):
    """新插件含 gateways 组件 → _reload_gateways 被分派（stop+unload/reload+rebuild）"""
    mgr, watcher = gateway_reload_spies
    _install_pm(monkeypatch, {"gateways": True}, "gateway-feishu")
    backend = _build_backend()

    result = backend._reload_new_plugin("gateway-feishu")

    assert result.get("gateways") is True, "gateways 组件必须被 __NEW__ 路径分派"
    # stop（清场）+ reload_plugin（注册 def）+ rebuild（建 adapter）全链命中
    assert mgr.stop_plugin_platforms.called
    assert watcher.reload_plugin.call_args.args == ("gateway-feishu",)
    assert mgr.rebuild_plugin_platforms.called


def test_reload_new_plugin_dispatches_storages_and_serializers(monkeypatch):
    """同构缺陷连带验证：storages/serializers 也在 __NEW__ 分派集合内"""
    from app.plugins.loaders import runtime_component_loader as rcl

    storage_watcher = MagicMock()
    serializer_watcher = MagicMock()
    monkeypatch.setattr(rcl, "ensure_storage_watcher", lambda: storage_watcher)
    monkeypatch.setattr(rcl, "ensure_serializer_watcher", lambda: serializer_watcher)
    _install_pm(monkeypatch, {"storages": True, "serializers": True}, "runtime-demo")
    backend = _build_backend()

    result = backend._reload_new_plugin("runtime-demo")

    assert result.get("storages") is True
    assert result.get("serializers") is True
    assert storage_watcher.reload_plugin.call_args.args == ("runtime-demo",)
    assert serializer_watcher.reload_plugin.call_args.args == ("runtime-demo",)
