# -*- coding: utf-8 -*-
"""gateways 组件类型：kernel 登记 + loader 扫描 + reloader 行为。

E2 Task 3：对称 serializers 模式，验证插件骨架网关组件类型已接入。
"""

from __future__ import annotations

import pytest

from app.plugins import kernel


class TestKernelRegistration:
    def test_known_components_contains_gateways(self):
        assert "gateways" in kernel.KNOWN_COMPONENTS
        assert "gateways" in kernel.COMPONENT_ORDER

    def test_gateways_after_serializers(self):
        """gateways 在 serializers 之后（网络进程组件生命周期独立于会话组件）"""
        order = list(kernel.COMPONENT_ORDER)
        assert order.index("gateways") > order.index("serializers")


@pytest.fixture()
def _patch_plugin_loader(monkeypatch, tmp_path):
    """构造独立插件根 + 旁路 _is_plugin_enabled（与运行时 loader 一致即可）"""
    from app.plugins.loaders import runtime_component_loader as rcl

    monkeypatch.setattr(
        rcl,
        "_plugin_roots",
        lambda: [tmp_path / "plugins"],
    )
    # 旁路 PM 启用状态判断：测试只验扫描 + 注册语义，不卷入 PM
    monkeypatch.setattr(rcl, "_is_plugin_enabled", lambda name: True)


class TestLoaderScansGatewayPlugins:
    def test_scan_registers_def_with_source(self, tmp_path, monkeypatch, _patch_plugin_loader):
        """构造临时插件根：tmp/plugins/p-gw/gateways/mypt.py"""
        plug = tmp_path / "plugins" / "p-gw" / "gateways"
        plug.mkdir(parents=True)
        (plug / "mypt.py").write_text(
            "# -*- coding: utf-8 -*-\n"
            "from app.plugins.contracts.gateway_platform import GatewayPlatformDef\n"
            "\n"
            "\n"
            "def _factory(cfg):\n"
            "    return object()\n"
            "\n"
            "\n"
            "def register(registry):\n"
            "    registry.register(GatewayPlatformDef(\n"
            "        platform_id='mypt', display_name='My PT',\n"
            "        adapter_factory=_factory,\n"
            "    ))\n",
            encoding="utf-8",
        )
        from app.plugins.loaders.runtime_component_loader import RuntimeComponentLoader
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

        reg = GatewayPlatformRegistry()
        try:
            loader = RuntimeComponentLoader("gateways", reg)
            found = loader.scan_roots()
            assert "p-gw" in found
            d = reg.get("mypt")
            assert d is not None
            assert d.source == "plugin:p-gw"
        finally:
            reg.unregister_source("plugin:p-gw")

    def test_watcher_underscore_ignored(self, tmp_path, monkeypatch, _patch_plugin_loader):
        """_shared.py 不被扫描（对齐既有 loader 约定：下划线前缀跳过）"""
        plug = tmp_path / "plugins" / "p2" / "gateways"
        plug.mkdir(parents=True)
        (plug / "_shared.py").write_text(
            "# -*- coding: utf-8 -*-\n"
            "from app.plugins.contracts.gateway_platform import GatewayPlatformDef\n"
            "\n"
            "\n"
            "def _factory(cfg):\n"
            "    return object()\n"
            "\n"
            "\n"
            "def register(registry):\n"
            "    registry.register(GatewayPlatformDef(\n"
            "        platform_id='shared_bad', display_name='Shared Bad',\n"
            "        adapter_factory=_factory,\n"
            "    ))\n",
            encoding="utf-8",
        )
        (plug / "real.py").write_text(
            "# -*- coding: utf-8 -*-\n"
            "from app.plugins.contracts.gateway_platform import GatewayPlatformDef\n"
            "\n"
            "\n"
            "def _factory(cfg):\n"
            "    return object()\n"
            "\n"
            "\n"
            "def register(registry):\n"
            "    registry.register(GatewayPlatformDef(\n"
            "        platform_id='real', display_name='Real',\n"
            "        adapter_factory=_factory,\n"
            "    ))\n",
            encoding="utf-8",
        )
        from app.plugins.loaders.runtime_component_loader import RuntimeComponentLoader
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

        reg = GatewayPlatformRegistry()
        try:
            loader = RuntimeComponentLoader("gateways", reg)
            loader.scan_roots()
            assert reg.get("shared_bad") is None, "_shared.py 应被扫描器跳过"
            assert reg.get("real") is not None
        finally:
            reg.unregister_source("plugin:p2")


class TestReloaderRegistered:
    def test_reload_gateways_registered_in_builtin(self):
        """builtin_reloaders 注册的 mapping 含 gateways（与 kernel 对齐）"""
        from app.plugins import builtin_reloaders as br
        from app.plugins.kernel import ComponentReloaderRegistry, ReloadContext

        reg = ComponentReloaderRegistry()
        # 重建以绕过幂等保护
        br._BUILTIN_REGISTERED.discard(id(reg))
        br.register_builtin_reloaders(reg)
        # 构造 ctx 触发
        ctx = ReloadContext(plugin_name="p", plugin=None, component="gateways", is_new_plugin=False)
        result = reg.reload(ctx)
        # 删除路径扫描结果为 False（无 watcher 时）/True（有 watcher 时）；不强求 bool，仅确保不抛
        assert result is not None
