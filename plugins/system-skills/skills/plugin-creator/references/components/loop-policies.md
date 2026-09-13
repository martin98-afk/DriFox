---
description: Loop Policies（循环策略）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# Loop Policies（循环策略）组件开发

控制一个对话引擎"工具循环怎么终止"：每轮结束判定续不继续、轮数上限、子智能体达到上限后的总结提示词。与 hook_policies 同构（scope 分域注册表 + 激活槽 + 按 id 直取）。

### 文件位置

```
your-plugin/
├── .drifox-plugin/plugin.json   ← components.loop_policies: true
└── loop_policies/*.py           ← 每个文件暴露 register(registry)
```

### 契约接口（app/plugins/contracts/loop_policy.py）

```python
class LoopDecision(Enum): CONTINUE = "continue"; STOP = "stop"

@dataclass
class LoopState:                      # 每轮构造一次
    round_count: int = 0
    tool_calls_found: bool = False          # 本轮有工具调用 → 通常 CONTINUE
    stop_hook_injected: bool = False        # Stop hook 续命一轮的机会
    repetitive_loop_detected: bool = False  # 连续相同工具调用

class LoopPolicy(Protocol):
    id: str
    scope: str  # main / subagent
    def should_continue(self, state: LoopState) -> LoopDecision: ...
    def max_rounds(self, llm_config) -> Optional[int]: ...  # None=不限；读 llm_config["最大循环轮数"] 对齐 default
    def final_summary_prompt(self) -> str: ...  # 仅 subagent 域需要（超限总结提示词）
```

### 最小模板

```python
# loop_policies/two_step.py
# -*- coding: utf-8 -*-
"""放行工具迭代 1 次，第 2 次后强制 STOP"""
from app.plugins.contracts.loop_policy import LoopDecision, LoopState

class TwoStepLoopPolicy:
    id = "two_step"
    scope = "main"

    def should_continue(self, state: LoopState) -> LoopDecision:
        return LoopDecision.CONTINUE if state.tool_calls_found else LoopDecision.STOP

    def max_rounds(self, llm_config) -> Optional[int]:
        return 2

def register(registry):
    registry.register(TwoStepLoopPolicy())
```

### 激活方式（两种，语义不同）

- **引擎级（推荐，不动全局）**：`create_engine_session("my-engine", loop_policy_id="two_step")` —— worker 按 id 直取，主对话不受影响
- **全局切换**：`LoopPolicyRegistry.get_instance().set_active("two_step")` —— 影响整个 main 域（DSH 极简模式做法），慎用

### 关键约束

- 子智能体域（subagent_worker）**只读全局激活槽**，不支持 per-agent loop_policy_id；`final_summary_prompt` 仅该域消费
- 策略调用异常安全：should_continue 抛异常回退 CONTINUE，max_rounds 回退不限——策略内部别依赖异常做控制流
- 系统默认：default（对齐原行为）/ minimal（单轮即停）/ subagent（默认 30 轮）

### 参考

- 契约：`app/plugins/contracts/loop_policy.py`；注册表：`app/plugins/registries/loop_policy_registry.py`
- 系统案例：`plugins/system-loop-policies/loop_policies/`
- 引擎级案例：`plugins/assistant_hub/loop_policies/single_turn.py`（单回合钳制，记忆整理场景）
