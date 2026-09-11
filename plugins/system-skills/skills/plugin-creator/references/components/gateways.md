---
description: Gateways（消息网关平台适配器）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# Gateways（消息网关平台适配器）组件开发

接入新通讯平台（钉钉/QQ/Telegram…）。三段式：依赖检测 + Adapter 实现 + GatewayPlatformDef 注册。

### 文件位置

```
your-plugin/
├── .drifox-plugin/plugin.json   ← components.gateways: true + config_schema
├── deps/                        # 平台 SDK（loader 自动注入 sys.path）
└── gateways/*.py
```

### 最小模板

```python
# gateways/mygw.py
from app.gateway.base import (
    BasePlatformAdapter, MessageEvent, Platform, PlatformConfig, SendResult, ChatInfo,
)

def check_requirements() -> bool:
    try:
        import some_sdk  # noqa
        return True
    except ImportError:
        return False

class MyAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 4096

    def __init__(self, config: PlatformConfig):
        super().__init__(config)              # 平台标识经 platform_id= 或类属性
        self._token = (config.extra or {}).get("token") or ""

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
        adapter_factory=lambda cfg: MyAdapter(cfg),
        check_requirements=check_requirements,
        ui_order=80,
    ))
```

### 关键约束

- `BasePlatformAdapter.__init__` 第二参数已修：None / 有 `.handle()` 的 handler / 平台标识三种语义鸭子类型分流（旧写法 `super().__init__(config, Platform.XXX)` 会把平台标识当 handler 丢弃 → `adapter.platform` 恒为 WECOM）
- 收发闭环：入站消息经 `MessageHandler.handle`（session 定位 → process_message_callback → 回发）；出站主动投递走 `GatewayService.send_to_platform`（插件从 services 拿，别 import 模块级单例）
- 配置走 `config_schema` + `PluginConfigStore`（E1 契约，设置卡自动渲染）；变更经 `build_config_values` 热重建 adapter

### 参考

- 契约：`app/gateway/base.py` / `app/plugins/contracts/gateway_platform.py`
- 完整案例：`drifox-plugins2` 仓库 `plugins/gateway-feishu/gateways/feishu.py`
