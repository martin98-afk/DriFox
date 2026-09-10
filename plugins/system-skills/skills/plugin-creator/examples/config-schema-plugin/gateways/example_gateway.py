# -*- coding: utf-8 -*-
"""带 config_schema 的示例插件 — gateways/example_gateway.py

参照自真实插件 gateway-qq（~/.drifox/plugins/gateway-qq/gateways/qq.py 尾部注册段），
Adapter 收发逻辑全部省略，只保留 E1 配置契约的完整链路。

链路全景：
  plugin.json config_schema  ──渲染──▶ 设置卡（主程序按字段类型生成表单）
        ▲                                        │ 用户点保存
        │                                        ▼
  PluginConfigStore（插件自包含存储）◀── _build_config_values(values, old)
        │ 读
        ▼
  _build_config() ──▶ PlatformConfig ──▶ adapter_factory(cfg) 创建适配器

三个回调都建议「闭包内延迟 import」，避免模块顶层触发宿主副作用。
本文件可整体复制改名后使用；需同步修改处见 [改名] 标记。
"""

# Adapter 骨架：真实插件要实现收发消息，此处仅示意构造注入。
# class ExampleAdapter(BasePlatformAdapter):
#     def __init__(self, config: PlatformConfig, **kwargs):
#         super().__init__(config, **kwargs)
#         self._endpoint = config.websocket_url or "wss://example.com/ws"
#         self._secret = config.secret or ""


# ── 读：PlatformConfig ← PluginConfigStore ──────────────
def _build_config() -> "PlatformConfig":
    """读 PluginConfigStore 构造平台配置（适配器创建时被调用）"""
    from app.gateway.base import PlatformConfig
    from app.plugins.managers.plugin_config_store import PluginConfigStore

    store = PluginConfigStore()
    return PlatformConfig(
        enabled=bool(store.get("config-schema-example", "enabled")),
        platform="example",  # [改名] 平台标识
        bot_id="",           # [按需] 平台账号标识，从 store 取
        secret=store.get("config-schema-example", "api_secret") or "",
        websocket_url=(
            store.get("config-schema-example", "endpoint")
            or "wss://example.com/ws"
        ),
    )


# ── 写：PlatformConfig → PluginConfigStore ──────────────
def _write_config(config) -> None:
    from app.plugins.managers.plugin_config_store import PluginConfigStore

    PluginConfigStore().set_values(
        "config-schema-example",  # [改名] 与 plugin.json name 一致
        {
            "enabled": config.enabled,
            "api_secret": config.secret or "",
            "endpoint": config.websocket_url or "",
        },
    )


# ── 设置卡保存回调：表单值 → 存储 → PlatformConfig ──────
def _build_config_values(values: dict, old_config) -> "PlatformConfig":
    from app.gateway.base import PlatformConfig
    from app.plugins.managers.plugin_config_store import PluginConfigStore

    store = PluginConfigStore()
    plugin = "config-schema-example"  # [改名]
    enabled = values.get("enabled", store.get(plugin, "enabled"))
    secret = values.get("api_secret", "")
    endpoint = values.get("endpoint", "")
    interval = values.get("reconnect_interval", 30)
    store.set_values(
        plugin,
        {
            "enabled": bool(enabled),
            "api_secret": secret,
            "endpoint": endpoint or "wss://example.com/ws",
            "reconnect_interval": interval,
        },
    )
    return PlatformConfig(
        enabled=bool(enabled),
        platform="example",  # [改名]
        bot_id="",
        secret=secret,
        websocket_url=endpoint or "wss://example.com/ws",
    )


def register(registry) -> None:
    """网关组件注册入口。validate_config 返回 (是否可启用, 未配置原因)。"""
    from app.plugins.contracts.gateway_platform import GatewayPlatformDef

    registry.register(
        GatewayPlatformDef(
            platform_id="example",        # [改名] 平台唯一标识
            display_name="Example",       # [改名] 界面展示名
            adapter_factory=lambda cfg: None,  # [必改] 换成真实 Adapter 类
            check_requirements=lambda: (True, ""),  # [按需] 依赖检查
            config_builder=_build_config,
            config_writer=_write_config,
            build_config_values=_build_config_values,
            validate_config=lambda cfg: (
                bool(cfg.secret),
                "Secret 未配置",
            ),
            ui_order=99,
        )
    )
