---
description: Serializers（消息序列化器）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# Serializers（消息序列化器）组件开发

把消息流序列化成不同 LLM 协议的请求格式（openai chat / responses / 自定义）。worker 统一走 `serializer.serialize()` 单入口，按 `ModelAdapter.ProtocolFlags.serializer_id` 命中。

### 文件位置

```
your-plugin/
├── .drifox-plugin/plugin.json   ← components.serializers: true
└── serializers/*.py
```

### 最小模板

```python
# serializers/passthrough.py
from app.plugins.contracts.serializer import SerializeContext

class PassthroughSerializer:
    id = "passthrough"

    def serialize(self, messages, ctx: SerializeContext):
        items, instructions = [], []
        # ... 把 messages 转成目标协议格式
        return items, "\n\n".join(instructions)

def register(registry):
    registry.register(PassthroughSerializer())
```

### 关键约束

- 生效需配套 `ModelAdapter` 的 `ProtocolFlags.serializer_id="passthrough"`（默认 "openai"），单独注册不会被选中
- 旧入口 `messages_to_api` 等仍保留，内部已委托 SerializerRegistry——别绕过单入口

### 参考

- 契约：`app/plugins/contracts/serializer.py`；注册表：`app/plugins/registries/serializer_registry.py`
- 消费：`app/core/workers/chat_worker.py` `_serialize_for_api()`
