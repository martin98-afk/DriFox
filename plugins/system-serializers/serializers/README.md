# serializers（消息序列化器）

运行时组件之一（万物即插件 Phase B）。把 `app/core/message_content.py` 的协议特判
序列化逻辑收敛为可插拔组件：**adapter（决策层）决定「协议开关」，serializer（执行层）
决定「消息形态」**。

## 目录约定

- 插件目录下放 `serializers/*.py`，每文件暴露 `register(registry)`（与
  tools/providers 对称），注册项带 `id` 属性 + 两个序列化方法。
- user 根可覆盖 system 根同名实现（`user > system`），热重载 watcher 自动生效。

## 契约

`app/plugins/contracts/message_serializer.py`：

- `MessageSerializer`（Protocol）：`id: str` + `serialize_messages(messages, ctx) -> List[Dict]`
  （等价旧 `messages_to_api`）+ `serialize_responses(messages, ctx) -> tuple`
  （等价旧 `messages_to_responses_input`，返回 `(input_items, instructions)`）。
- `SerializeContext`：`supports_vision`（模型能力，worker 注入）+ `flags`
  （`ProtocolFlags`，由 ModelAdapter 解析）。

## 默认实现

`plugins/system/serializers/openai.py`（id=`"openai"`）：逻辑与旧实现逐点等价，
辅助函数复用 `app.core.message_content`（`normalize_message` /
`_extract_content_for_api` / `_build_api_tool_call` / `_prune_tool_content_for_api` /
`_extract_responses_content`）。

## 消费入口

`message_content.to_api_message / messages_to_api / messages_to_responses_input`
为薄壳，统一经 `SerializerRegistry.get_instance().resolve()`（无该 id 回退 `openai`）
委托。`ProtocolFlags.serializer_id` 字段已立（默认 `openai`），Phase B 走
「覆盖式替换」（注册同 id 覆盖），Phase C 再做「worker 单入口 + adapter 指定序列化策略」。

## 示例

```python
class MySerializer:
    id = "my-serializer"

    def serialize_messages(self, messages, ctx):
        return [{"role": "user", "content": "custom"}]

    def serialize_responses(self, messages, ctx):
        return ([], "custom")

def register(registry):
    registry.register(MySerializer())
```
