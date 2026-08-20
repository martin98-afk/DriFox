# -*- coding: utf-8 -*-
"""E2E：第三方平台纯插件目录接入（零主程序改动证明）。

链路（E2 Task 7）：
    临时 user 插件根 → RuntimeComponentLoader.scan_roots 扫描
      → GatewayPlatformRegistry 注册（source='plugin:pt-awesome'）
        → PlatformManager._load_adapters 拾取 adapter
          → def.config_builder 提供配置（token='e2e-token'）

Step B 补一条端到端 config 链：第三方平台 def.config_builder 经
E1 `config_schema` 声明 + `PluginConfigStore` 读取存储值，验证
「配置存储→加载→构造」全链（不依赖主程序 Settings）。

对齐既有模式：
- `_plugin_roots` monkeypatch（tests/plugins/gateways/test_gateway_component_plumbing.py）
- `RuntimeComponentLoader('gateways', reg).scan_roots()` 直接构造（同上）
- 不新建进程级 watcher，避免污染其他测试全局状态
- `PluginConfigRegistry.get_instance()` 模拟 `PluginManager._register_config_schema`
  （tests/plugins/test_websearch_config_contract.py 同款 fixture 思路）
"""

from __future__ import annotations

import pytest


# ──────────────────────────────────────────────────────────────────────
# 共享 fixture：临时第三方插件（gateways/pt_awesome.py + plugin.json）
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture()
def third_party_plugin(tmp_path, monkeypatch):
    """构造一个临时第三方 user 插件根（point _plugin_roots 到 tmp_path/plugins）。"""
    plug = tmp_path / "plugins" / "pt-awesome" / "gateways"
    plug.mkdir(parents=True)
    # 平台模块：注册 GatewayPlatformDef
    (plug / "pt_awesome.py").write_text(
        "# -*- coding: utf-8 -*-\n"
        "from app.plugins.contracts.gateway_platform import GatewayPlatformDef\n"
        "from app.gateway.base import PlatformConfig\n"
        "\n"
        "class PtAwesomeAdapter:\n"
        "    platform = 'pt-awesome'\n"
        "    def __init__(self, config):\n"
        "        self.config = config\n"
        "\n"
        "def register(registry):\n"
        "    registry.register(GatewayPlatformDef(\n"
        "        platform_id='pt-awesome', display_name='PT Awesome',\n"
        "        adapter_factory=lambda cfg: PtAwesomeAdapter(cfg),\n"
        "        config_builder=lambda: PlatformConfig(enabled=True, platform='pt-awesome',\n"
        "                                               token='e2e-token'),\n"
        "    ))\n",
        encoding="utf-8",
    )
    # plugin.json：声明 components.gateways
    manifest_dir = tmp_path / "plugins" / "pt-awesome" / ".drifox-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "plugin.json").write_text(
        '{"name": "pt-awesome", "version": "1.0.0", "components": {"gateways": true}}',
        encoding="utf-8",
    )
    # 旁路 _is_plugin_enabled（测试只验扫描 + 注册语义，不卷入 PluginManager）
    from app.plugins.loaders import runtime_component_loader as rcl

    monkeypatch.setattr(rcl, "_plugin_roots", lambda: [tmp_path / "plugins"])
    monkeypatch.setattr(rcl, "_is_plugin_enabled", lambda name: True)
    # 隔离 app_data_dir：避免污染真实用户目录
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    yield tmp_path
    from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

    GatewayPlatformRegistry.get_instance().unregister_source("plugin:pt-awesome")


def _loader_for_gateways():
    """构造独立的 RuntimeComponentLoader 实例（不触发全局 watcher）。"""
    from app.plugins.loaders.runtime_component_loader import RuntimeComponentLoader
    from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

    return RuntimeComponentLoader("gateways", GatewayPlatformRegistry.get_instance())


# ──────────────────────────────────────────────────────────────────────
# Step A：第三方平台从临时目录被 loader 扫描到 manager 拾取的全链
# ──────────────────────────────────────────────────────────────────────


class TestThirdPartyFullChain:
    def test_loader_scans_and_registers_with_source(self, third_party_plugin):
        """loader 扫描 → registry 注册（source='plugin:pt-awesome'）。"""
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

        reg = GatewayPlatformRegistry.get_instance()
        loaded = _loader_for_gateways().scan_roots()
        assert "pt-awesome" in loaded

        d = reg.get("pt-awesome")
        assert d is not None
        assert d.source == "plugin:pt-awesome"
        assert d.display_name == "PT Awesome"

    def test_manager_picks_up_third_party_adapter(self, third_party_plugin):
        """PlatformManager._load_adapters 拾取 → adapter 实例持有 builder 提供的 token。"""
        from app.gateway.config import GatewayConfigHelper
        from app.gateway.manager import PlatformManager
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

        # 1) 扫临时插件目录 → 注册 def
        _loader_for_gateways().scan_roots()
        assert GatewayPlatformRegistry.get_instance().get("pt-awesome") is not None

        # 2) 绕过 PlatformManager.__init__（避免启线程/事件循环/session_manager）
        #    仅补齐 _load_adapters 依赖：_adapters 容器 + _config（get_platform_config 静态门面）
        mgr = PlatformManager.__new__(PlatformManager)
        mgr._adapters = {}
        mgr._config = GatewayConfigHelper
        PlatformManager._load_adapters(mgr)

        # 3) 断言：第三方平台已注入，且 builder 提供的 token 透传到 adapter
        assert "pt-awesome" in mgr._adapters, "PlatformManager 未拾取第三方平台 adapter"
        adapter = mgr._adapters["pt-awesome"]
        assert adapter.config.token == "e2e-token", "config_builder 提供的 token 未透传"


# ──────────────────────────────────────────────────────────────────────
# Step B：端到端 config 链（E1 config_schema + PluginConfigStore 写入 + builder 读取）
# ──────────────────────────────────────────────────────────────────────


class TestThirdPartyConfigChain:
    def test_config_schema_to_constructor_roundtrip(self, third_party_plugin):
        """端到端 config 链：config_schema 字段 → set_values 写入 → builder 经 store 读出。

        第三方平台正确接入形态：plugin.json 声明 `config_schema`，主程序自动渲染
        设置卡 + PluginConfigStore 统一存储；def.config_builder 从 store 读取
        实际用户配置（不依赖主程序 Settings，与内置 6 平台闭包桥接 Settings 区分）。

        本测试不走完整 PluginManager 链路（避免卷入插件目录扫描耦合），改为：
          - 直接把 schema 注册到 PluginConfigRegistry（与 PluginManager._register_config_schema 等价）
          - 通过 PluginConfigStore.set_values 写入临时 app_data_dir
          - 用 builder 读 store.get(key)，断言读出的 token 与写入一致
        """
        from app.plugins.contracts.plugin_config import parse_config_schema
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry
        from app.plugins.registries.plugin_config_registry import PluginConfigRegistry

        # 1) 注册 config_schema（模拟 PluginManager 扫描 plugin.json 的行为）
        schema = parse_config_schema(
            "pt-awesome",
            {
                "title": "PT Awesome 配置",
                "fields": [
                    {
                        "key": "bot_token",
                        "label": "Bot Token",
                        "type": "password",
                        "default": "",
                        "env": "PT_AWESOME_BOT_TOKEN",
                        "placeholder": "xoxb-...",
                    },
                ],
            },
        )
        PluginConfigRegistry.get_instance().register(schema)
        try:
            # 2) 第三方插件 def：builder 从 PluginConfigStore 读
            from app.plugins.loaders import runtime_component_loader as rcl

            rcl._plugin_roots()  # sanity: fixture 已 monkeypatch

            from app.gateway.base import PlatformConfig
            from app.plugins.contracts.gateway_platform import GatewayPlatformDef

            def _config_builder():
                from app.plugins.managers.plugin_config_store import PluginConfigStore

                store = PluginConfigStore()
                return PlatformConfig(
                    enabled=True,
                    platform="pt-awesome",
                    token=store.get("pt-awesome", "bot_token") or "e2e-fallback",
                )

            # 3) 写入用户配置（模拟设置面板保存）
            from app.plugins.managers.plugin_config_store import PluginConfigStore

            PluginConfigStore().set_values("pt-awesome", {"bot_token": "e2e-user-token"})

            # 4) 注册到 registry（不扫 loader，直接 register，聚焦 config 链路）
            reg = GatewayPlatformRegistry.get_instance()
            reg.register(
                GatewayPlatformDef(
                    platform_id="pt-awesome",
                    display_name="PT Awesome",
                    adapter_factory=lambda cfg: object(),
                    config_builder=_config_builder,
                ),
                source="plugin:pt-awesome",
            )
            # 5) builder 读出写入值（builder 经 store 三级链：env→存储→默认）
            d = reg.get("pt-awesome")
            assert d is not None
            cfg = d.config_builder()
            assert cfg.token == "e2e-user-token", "builder 未从 PluginConfigStore 读取写入值"
            assert cfg.enabled is True
        finally:
            PluginConfigRegistry.get_instance().unregister_plugin("pt-awesome")


# ──────────────────────────────────────────────────────────────────────
# Step A 续：user 根覆盖 system 根（跨根规则契约）
# ──────────────────────────────────────────────────────────────────────


class TestUserOverridesSystem:
    def test_scan_roots_yields_def_via_user_root(self, third_party_plugin):
        """user 根（fixture monkeypatch 指向 tmp_path/plugins）扫描后注册生效。

        跨根 user>system 覆盖规则由 `_RegistryProxy` 在 runtime loader 层保证，
        既有 tests/plugins/gateways/test_gateway_component_plumbing.py 已独立覆盖
        loader.proxy 的 occupied 覆盖判定。本测试仅断言扫描行为契约不变：
        临时 user 根下第三方插件经 scan_roots 后出现在 registry 中。
        """
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

        _loader_for_gateways().scan_roots()
        d = GatewayPlatformRegistry.get_instance().get("pt-awesome")
        assert d is not None
        assert d.source == "plugin:pt-awesome"
