---
description: Gateways（消息网关平台适配器）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# Gateways（消息网关平台适配器）组件开发

接入新通讯平台（钉钉/QQ/Telegram…）。三段式：依赖检测 + Adapter 实现 + GatewayPlatformDef 注册。

平台 SDK 等第三方依赖写 manifest `dependencies.pip`（CI 打包自动进 tarball，仓库不放 deps/）；`check_requirements()` 只做 import 探测。本地开发把依赖装到插件 `deps/<platform>/` 自测可以，勿提交。

### 文件位置

```
your-plugin/
├── .drifox-plugin/plugin.json   ← components.gateways: true + config_schema + dependencies.pip
└── gateways/*.py
```

### 最小模板
```python
# gateways/mygw.py
from app.gateway.base import BasePlatformAdapter, ChatInfo, MessageEvent, SendResult

PLUGIN_NAME = "my-gateway"
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
    reader = _cap("plugin.config.read")
    values = (reader() if reader else {}) or {}
    return {"enabled": bool(values.get("enabled")), "token": values.get("token") or ""}


def _set_enabled(enabled: bool) -> None:
    writer = _cap("plugin.config.write")
    if writer is not None:
        writer("enabled", bool(enabled))


def _validate() -> tuple:
    return bool(_config()["token"]), "Token 未配置"


class MyAdapter(BasePlatformAdapter):
    platform_id = "mygw"          # registry 键，子类声明
    name = "MyGW"
    MAX_MESSAGE_LENGTH = 4096

    def __init__(self) -> None:
        super().__init__()         # 无参：配置由插件自己读
        self._token = _config()["token"]

    async def connect(self) -> bool: ...
    async def disconnect(self) -> None: ...
    async def send(self, chat_id, content, **kw) -> SendResult:
        return SendResult(success=True)
    async def get_chat_info(self, chat_id) -> ChatInfo:
        return ChatInfo(chat_id=chat_id)


def register(registry):
    from app.plugins.contracts.gateway_platform import GatewayPlatformDef

    registry.register(GatewayPlatformDef(
        platform_id="mygw", display_name="MyGW",
        adapter_factory=MyAdapter,
        is_enabled=lambda: _config()["enabled"],
        set_enabled=_set_enabled,
        validate=_validate,
        ui_order=80,
    ))
```
manifest 至少声明 `components.gateways` 与
`capabilities: ["plugin.config.read", "plugin.config.write"]`。

### 关键约束
- **宿主契约不承载配置**：`PlatformConfig` / `Platform` 枚举均已删除，`MessageEvent.platform`
  与 `BasePlatformAdapter.platform_id` 都是 `str`（第三方平台与内置平台同构）
- `BasePlatformAdapter.__init__()` 无参；平台标识由子类类属性 `platform_id` 声明
- 配置统一走 `config_schema` + `plugin.config.read/write` 能力（设置卡自动渲染，插件自己读同一份存储）
- 宿主只问三个问题：`is_enabled()`（要不要连）、`validate()`（配置能不能用）、`adapter_factory()`（造哪个实例）
- 收发闭环：入站消息经 `MessageHandler.handle`；出站主动投递走 `platform.send` 能力

### 参考

- 契约：`app/gateway/base.py` / `app/plugins/contracts/gateway_platform.py`
- 完整案例：`drifox-plugins2` 仓库 `plugins/gateway-feishu/gateways/feishu.py`
