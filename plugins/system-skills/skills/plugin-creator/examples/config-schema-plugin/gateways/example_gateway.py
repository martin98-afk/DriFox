# -*- coding: utf-8 -*-
"""带 config_schema 的示例插件 — gateways/example_gateway.py

参照真实插件 gateway-qq（~/.drifox/plugins/gateway-qq/gateways/qq.py 尾部注册段）。
Adapter 收发逻辑全部省略，只保留配置与注册契约的完整链路。

链路全景（宿主不承载任何平台配置字段）：

  plugin.json config_schema ──渲染──▶ 设置卡（主程序按字段类型生成表单）
        ▲                                     │ 用户点保存
        │                                     ▼
  PluginConfigStore（插件自包含存储）◀── 宿主写入（plugin.config.write 能力）
        │ 读（plugin.config.read 能力）
        ▼
  _config() ──▶ dict ──▶ Adapter.__init__ 自读 / is_enabled / validate

注册只给行为回调：`adapter_factory`（无参）、`is_enabled`、`set_enabled`、
`validate`、`check_requirements`。

本文件可整体复制改名后使用；需同步修改处见 [改名] 标记。
"""

PLUGIN_NAME = "config-schema-example"  # [改名] 与 plugin.json name 一致

_CAPS: dict = {}


def _cap(cap_id: str):
    """取宿主能力（按 manifest capabilities 裁剪；后台线程无 ctx 也可用）。"""
    cap = _CAPS.get(cap_id)
    if cap is None:
        from app.plugins.sdk import caps_for

        cap = caps_for(cap_id, plugin=PLUGIN_NAME, surface="ui")
        if cap is None:
            return None
        _CAPS[cap_id] = cap
    return cap


def _config() -> dict:
    """读本插件配置（plugin.config.read 能力，一次取全量）。"""
    reader = _cap("plugin.config.read")
    values = (reader() if reader else {}) or {}
    return {
        "enabled": bool(values.get("enabled")),
        "api_secret": values.get("api_secret") or "",
        "endpoint": values.get("endpoint") or "wss://example.com/ws",  # [按需]
        "reconnect_interval": int(values.get("reconnect_interval") or 30),
    }


def _set_enabled(enabled: bool) -> None:
    writer = _cap("plugin.config.write")
    if writer is not None:
        writer("enabled", bool(enabled))


def _validate() -> tuple:
    """返回 (是否可启用, 未配置原因)。"""
    return bool(_config()["api_secret"]), "Secret 未配置"


# Adapter 骨架：真实插件要实现收发消息，此处仅示意「自读配置」。
# class ExampleAdapter(BasePlatformAdapter):
#     platform_id = "example"          # [改名]
#     name = "Example"
#
#     def __init__(self) -> None:
#         super().__init__()
#         cfg = _config()
#         self._endpoint = cfg["endpoint"]
#         self._secret = cfg["api_secret"]


def register(registry) -> None:
    """网关组件注册入口：只给行为回调，配置由插件自管。"""
    from app.plugins.contracts.gateway_platform import GatewayPlatformDef

    registry.register(
        GatewayPlatformDef(
            platform_id="example",          # [改名] 平台唯一标识
            display_name="Example",         # [改名] 界面展示名
            adapter_factory=lambda: None,   # [必改] 换成无参 Adapter 类
            check_requirements=lambda: True,  # [按需] 依赖（SDK/deps）检查
            is_enabled=lambda: _config()["enabled"],
            set_enabled=_set_enabled,
            validate=_validate,
            ui_order=99,
        )
    )
