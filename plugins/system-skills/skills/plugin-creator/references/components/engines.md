---
description: Engines（对话引擎）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# Engines（对话引擎）组件开发

两类含义：① `engines/*.py` 组件替换主窗口对话引擎实现（进阶，少用）；② **插件驱动对话的标准姿势** —— 经 `services["create_engine_session"]` 拿 `EngineSession` 同步驱动一轮对话。后者是 90% 场景要看的。

### EngineSession 服务链

```
ctx["services"]["create_engine_session"](engine_name, **kwargs)
  → EngineSessionImpl（隔离 ConversationCore/SessionManager，不污染主窗口会话）
  → session.turn(...) -> ChatResult(text, error, cancelled, timed_out, messages)
```

**kwargs**：`hook_policy`（默认 NONE）/ `hook_policy_id`（显式策略，优先）/ `loop_policy_id`（引擎级循环策略）/ `permission_strategy`（"auto_allow"/"auto_deny"…）/ `model_config_override`（dict，顶层 merge，不锁模型切换）。

**turn 签名**：`turn(system=None, user=None, messages=None, tools=[], callbacks=None, timeout=300.0, auto_history=False)`。`ChatResult.ok` 为真时取 `.text`；`timed_out/cancelled/error` 分别处理。

### 最小 turn 模板（后台线程）

```python
create = services["create_engine_session"]
session = create("my-engine",
    hook_policy="none",                    # 防被动触发全局 hooks
    # hook_policy_id="my_id",              # 想精细接管才给
    # loop_policy_id="my_loop_id",         # 引擎级循环策略，不动全局槽
    permission_strategy="auto_allow",
)
try:
    r = session.turn(system="你是助手。", user="你好",
                     tools=[], timeout=300.0)
    if r.ok: use(r.text)
    elif r.timed_out: ...
finally:
    session.cleanup()                      # 插件停用时释放
```

### 关键约束

- **必须在非 UI 线程调用 turn**（阻塞式）；QThread/daemon 线程 + 轮询/看门狗是标准姿态（参考 cron-tasks executor：daemon 线程跑 turn + QThread 主体 200ms 轮询 + 硬看门狗，防流式挂死卡死收尾）
- `auto_history=True` 才由引擎存 history；默认插件自管上下文
- 真实消费者：`assistant_hub/core/llm_client.py`（会话池化）、`drifox-plugins2` 的 `cron-tasks/crontasks_core/executor.py`
- `autoloop` 走的是 `services["conversation_stack"]`（EP2 体系），不经 create_engine_session

### 参考

- 契约：`app/plugins/contracts/engine_session.py` / `engine_host.py`
- 实现：`app/core/conversation/engine_session.py`（含引擎级 hook 触发 `_trigger_engine_hook`）
- 引擎替换组件（进阶）：`engines/*.py` + `app/plugins/contracts/dialogue_engine.py` 的 `ClassEngineFactory(ENGINE_SLOT_UI, MyEngine)`，必须继承内置 UIEngine
