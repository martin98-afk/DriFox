---
description: Hook Policies（hook 触发策略）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# Hook Policies（hook 触发策略）组件开发

控制一个对话引擎"参与哪些 hook 节点"。主对话策略粒度太粗（all/tool_only/none）时，插件可自带精细策略，让引擎按配置文件逐节点开关 hook。

### 文件位置

```
your-plugin/
├── .drifox-plugin/plugin.json   ← components.hook_policies: true
└── hook_policies/*.py           ← 每个文件暴露 register(registry)
```

### 最小模板

```python
# hook_policies/my_selective.py
# -*- coding: utf-8 -*-
"""可配置事件白名单 hook 策略（id="my_selective"，scope="main"）"""
from app.plugins.contracts.hook_policy import HookDecision, HookEvent, SessionStartEvent, StopEvent


class MySelectiveHookPolicy:
    id = "my_selective"      # 全局唯一；create_engine_session 时引用
    scope = "main"           # main / subagent / team_member

    def should_trigger(self, event: HookEvent) -> HookDecision:
        if isinstance(event, SessionStartEvent):
            return HookDecision.TRIGGER
        return HookDecision.SKIP


def register(registry):
    registry.register(MySelectiveHookPolicy())
```

### 事件类型（contracts/hook_policy.py，isinstance 判定）

`SessionStartEvent` / `BuildSystemPromptEvent` / `PreUserMessageEvent` / `PostUserMessageEvent` / `PreAssistantMessageEvent` / `PostAssistantMessageEvent` / `PreToolUseEvent` / `PostToolUseEvent` / `StopEvent` / `PluginChangedEvent` / `TeamMailEvent`

### 触发点分工（谁消费你的策略）

| 事件 | 判定位置 |
|------|---------|
| SessionStart / BuildSystemPrompt / PreUserMessage / PostUserMessage | EngineSession（插件引擎链路；主对话走 backend/agent.py） |
| PreToolUse / PostToolUse / Stop / Pre(Actively)AssistantMessage | ChatWorker（`_should_run_hook`）+ tool_executor |
| PluginChanged / TeamMail | 进程级/团队级事件，不受会话策略控制 |

### 激活方式

```python
session = services["create_engine_session"]("my-engine", hook_policy_id="my_selective")
```

`hook_policy_id` 优先于 `hook_policy` 枚举（ALL/TOOL_EVENTS_ONLY/NONE 回落为 all/tool_only/none）。注册表见 `app/plugins/registries/hook_policy_registry.py`（按 scope 分域激活槽 + 回落链）。

### 关键约束

- 引擎会话不读 `session.messages`——hook 输出须进 prompt 才对 LLM 生效（EngineSession 已统一把 SessionStart/BuildSystemPrompt/PreUserMessage 贡献拼进 system）
- 配置驱动型策略：配置文件放 `<app_data>/plugin_data/<plugin>/`，mtime 缓存实现"改文件即生效"

### 参考

- 契约：`app/plugins/contracts/hook_policy.py`；注册表：`app/plugins/registries/hook_policy_registry.py`
- 系统案例：`plugins/system-hook-policies/hook_policies/`（all/none/tool_only/subagent_default/team_member）
- 配置驱动案例：`drifox-plugins2` 仓库 `plugins/cron-tasks/hook_policies/cron_selective.py`
- 引擎侧触发实现：`app/core/conversation/engine_session.py` 的 `_trigger_engine_hook`
