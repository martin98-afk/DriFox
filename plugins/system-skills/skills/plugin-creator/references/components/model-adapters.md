---
description: Model Adapters（模型协议适配器）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# Model Adapters（模型协议适配器）组件开发

告诉 worker "当前模型配置该用什么协议行为"（chat vs responses、reasoning_content 回传、serializer_id 等）。**不读不写 `_valid_configs`**——Provider（providers 组件）管配置录入，ModelAdapter 只在运行时按 `llm_config` 打分解析协议。

### 文件位置

```
your-plugin/
├── .drifox-plugin/plugin.json   ← components.model_adapters: true
└── model_adapters/*.py
```

### 契约与机制

```python
class ModelAdapter(Protocol):
    id: str
    def matches(self, llm_config: Dict[str, Any]) -> int: ...   # 评分制，高分者胜
```

- `ModelAdapterRegistry.resolve(llm_config)`：遍历全部 adapter 取 `matches()` 最高分
- worker 每次请求前 resolve，按 adapter 的 `ProtocolFlags`（含 `serializer_id`，默认 "openai"）决定协议行为
- 无 UI、无 config_schema——纯代码注册；与 ProviderDef 完全解耦

### 参考

- 契约：`app/plugins/contracts/model_adapter.py`；注册表：`app/plugins/registries/model_adapter_registry.py`
- 系统案例：`plugins/system-model-adapters/model_adapters/`
- resolve 入口：`app/core/workers/chat_worker.py`（L3055 附近）
