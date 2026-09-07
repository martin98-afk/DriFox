# model_adapters（模型协议适配器）

运行时组件之一（万物即插件 Phase A/B/C）。**决策层**：把散落在 worker 的协议检测
分支收敛为可插拔决策——adapter 决定「协议开关」（`ProtocolFlags`），serializer
（执行层）决定「消息形态」。Phase C 起按**协议家族**拆分，可组合替换。

## 目录约定

- 插件目录下放 `model_adapters/*.py`，每文件暴露 `register(registry)`，注册项带
  `id` + `matches(llm_config) -> int`（0=不匹配，越大越优先）+ `protocol_flags(llm_config)`。
- `_` 前缀文件（如 `_detectors.py`）是共享工具模块，**不被 loader 当作插件**。
- user 根可覆盖 system 根同名实现（`user > system`），热重载自动生效。

## 系统默认三家族（plugins/system/model_adapters/）

| 家族 | id | matches | 归属 |
|---|---|---|---|
| DeepSeek | `deepseek-family` | 3（`detect_requires_reasoning` 命中） | deepseek 系 thinking mode（官方/中转，模型名以 deepseek 开头兜底） |
| Gemini | `gemini-family` | 2（`detect_is_gemini` 命中） | Gemini 模型（官方 provider 或模型名含 gemini） |
| OpenAI | `openai-family` | 1（恒兜底） | 其余全部（含 gpt-5 系走 Responses API） |

`ModelAdapterRegistry.resolve(cfg)` 取 `matches` 最高分家族；等价矩阵测试
（`tests/plugins/test_adapter_families.py`）保证拆分后 flags 与拆分前逐点等价。
判定器共享 `_detectors.py`（`detect_is_gemini / detect_requires_reasoning /
detect_use_responses`，逻辑逐字搬运自旧实现）。

## 序列化联动（Phase C 单入口）

worker 序列化不再直接调 `messages_to_api` 等薄壳函数，统一经
`_serialize_for_api(messages)` 单入口：

1. `_adapter_flags()` 解析协议开关（含 `serializer_id`）
2. `SerializerRegistry.resolve(flags.serializer_id)` 解析序列化器（默认 openai）
3. `serializer.serialize(messages, SerializeContext(flags=...)) -> SerializeResult`
   （内部按 `flags.use_responses_api` 路由 chat/responses 形态）

## 覆盖示例

```python
from app.plugins.contracts.model_adapter import ProtocolFlags
from plugins.system.model_adapters import _detectors as det


class CustomGeminiFamily:
    id = "gemini-family"  # 覆盖系统 gemini-family

    def matches(self, llm_config):
        return 2 if det.detect_is_gemini(llm_config) else 0

    def protocol_flags(self, llm_config):
        return ProtocolFlags(is_gemini=True, serializer_id="openai")


def register(registry):
    registry.register(CustomGeminiFamily())
```
