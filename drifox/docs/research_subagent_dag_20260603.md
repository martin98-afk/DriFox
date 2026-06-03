# 研究报告：AI Agent DAG 子智能体节点图执行方案竞品调研

**时间**：2026-06-03 15:33  
**模式**：deep  
**主题**：AI Agent 节点图执行方式（subagent_dag）的竞品分析

---

## 执行摘要

DriFox 的 `subagent_dag` 功能——通过有向无环图控制子智能体运行、后续节点自动获取前置节点上下文、支持生成 ECharts 节点图——在 2025-2026 年的 AI Agent 生态中已有多条成熟的技术路线与之对应。

**用户实际场景**：LLM 自主决策子智能体编排，LLM 直接生成 DAG 拓扑图，决定节点依赖关系和执行顺序。这一场景下，**Claude Code** 和 **OpenAI Codex** 是最直接的竞品。

| 竞品 | 编排方式 | 并行执行 | 上下文传递 | 可视化 |
|------|---------|---------|-----------|--------|
| **Claude Code** | LLM 自主决策（Task Tool） | ✅ fan-out/fan-in | 独立 context + 结果蒸馏 | Agent View（文本） |
| **OpenAI Codex** | LLM 自主决策（Manager-Worker） | ✅ 多类型 Subagent | Manager 收集结果 | trace 视图 |
| **DriFox** | LLM 生成 DAG 配置 | ✅ 入度为0并行 | context 自动注入 | **ECharts 节点图** |

**结论**：Claude Code 和 Codex 实现了 LLM 自主编排，但没有 ECharts 可视化；DriFox 的 ECharts 节点图是差异化特性。

---

## 1. Claude Code（Anthropic）— 最直接竞品

### 1.1 核心机制：Task Tool + Fan-Out/Fan-In 模式

Claude Code 的子智能体编排**完全由 LLM 自主决策**，不需要用户手动定义 DAG：

```
Main Claude → 任务分解（LLM 自主决策）→ 并行 Spawn Subagents → 收集结果 → 合并输出
```

**LLM 决策点**：
1. **何时 spawn**：Claude 自动识别可并行的任务，spawn subagent
2. **如何分解**：LLM 决定任务粒度（如"Agent 1 改 frontend, Agent 2 改 backend"）
3. **依赖处理**：LLM 识别依赖关系，决定串行/并行

### 1.2 关键特性对比

| 特性 | Claude Code 实现 | DriFox 对应 |
|------|-----------------|-------------|
| **并行执行** | ✅ Task Tool 支持多任务并行 | ✅ |
| **上下文隔离** | ✅ 每个 subagent 独立 context window | ✅ context 自动注入 |
| **结果蒸馏** | ✅ 返回摘要而非完整 transcript | ⚠️ 待设计 |
| **依赖管理** | ⚠️ 由 LLM 隐式处理（无显式 DAG） | ✅ 显式 edges |
| **Git Worktree 隔离** | ✅ `isolation: "worktree"` | ❌ 待实现 |
| **Agent Teams（实验）** | ✅ 支持多 Agent 直接通信 | ❌ 待设计 |
| **可视化** | ❌ Agent View 仅文本状态 | ✅ **ECharts 节点图** |

### 1.3 Agent 类型

| Agent 类型 | 用途 | 工具权限 |
|-----------|------|---------|
| **Explore Agent** | 只读，快速探索代码库 | glob, grep, read |
| **Plan Agent** | 只读，专注架构分析 | read only |
| **General Purpose Agent** | 完整工具集，实现代码 | full toolkit |

### 1.4 高级特性

**Git Worktree 隔离**：
```python
# spawn subagent with worktree isolation
Task(tool="parallel", isolation="worktree", ...)
```
防止多 Agent 同时编辑同一文件产生冲突。

**Agent Teams（实验性）**：
- 支持多 Agent 直接通信（不只是通过 parent 中转）
- 需要 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` 特性开关
- 适用于大型多组件项目，需要各部分在实现过程中保持同步

**来源**：
- [Claude Code 官方文档](https://code.claude.com/docs/en/agents)
- [Samanvya.dev 深度分析](https://samanvya.dev/blog/claude-code-subagents-parallel)
- [Turion.ai 完整指南](https://turion.ai/blog/claude-code-multi-agents-subagents-guide/)

---

## 2. OpenAI Codex（2026-03 GA）— 另一直接竞品

### 2.1 核心机制：Manager Agent 协调 + 多类型 Subagent

Codex 的 subagents 是 **2026-03 才 GA 的生产级功能**，采用 Manager-Worker 架构：

> "Codex can run subagent workflows by spawning specialized agents in parallel and then collecting their results in one response. Codex handles orchestration across agents, including spawning new subagents, routing follow-up instructions, waiting for results, and closing agent threads."

**LLM 决策点**：
- **何时 spawn**：Codex 根据任务复杂度**自动决定**是否需要 subagent
- **自定义 agent**：用户可定义 TOML 配置文件，Codex 自动选择合适的 agent
- **并行管理**：`agents.max_threads` 控制并发上限（默认 6）

### 2.2 内置 Agent 类型

| Agent | 用途 |
|-------|------|
| `default` | 通用 fallback |
| `worker` | 专注执行和修复 |
| `explorer` | 代码库探索（只读） |

### 2.3 自定义 Agent 配置

```toml
# ~/.codex/agents/reviewer.toml
name = "reviewer"
description = "PR reviewer focused on correctness, security, and missing tests."
developer_instructions = """
Review code like an owner.
Prioritize correctness, security, behavior regressions, and missing test coverage.
"""
nickname_candidates = ["Atlas", "Delta", "Echo"]
model = "o4"  # 可选：指定模型
sandbox_mode = "read_only"  # 可选：沙箱配置
```

### 2.4 与 DriFox 对比

| 维度 | Codex Subagents | DriFox subagent_dag |
|------|-----------------|---------------------|
| **编排触发** | LLM 自主决策 | LLM 生成 DAG 配置 |
| **节点定义** | TOML 配置文件 | `nodes` 数组 |
| **依赖描述** | 隐式（由 LLM 管理） | 显式 `edges` 数组 |
| **并行执行** | ✅ | ✅ |
| **上下文传递** | ✅ Manager 收集结果 | ✅ context 自动注入 |
| **可视化** | ❌（无 ECharts） | ✅ ECharts 节点图 |
| **沙箱隔离** | ✅ 独立云沙箱 | ❌ 待实现 |
| **并发控制** | `agents.max_threads`（默认6） | ⚠️ 待设计 |

**来源**：
- [OpenAI Codex Subagents 官方文档](https://developers.openai.com/codex/subagents)
- [Lushbinary Guide](https://lushbinary.com/blog/openai-codex-subagents-autonomous-coding-teams-guide/)

---

## 3. 框架层参考

### 3.1 LangGraph（最成熟的 DAG 框架）

| 维度 | LangGraph | DriFox subagent_dag |
|------|-----------|---------------------|
| **核心抽象** | StateGraph + Nodes + Edges | subagent_dag(nodes, edges) |
| **节点定义** | Python 函数 | 子智能体 ID + 描述 |
| **依赖描述** | `add_edge()` / `add_conditional_edges()` | `edges: [{from, to}]` |
| **并行执行** | ✅ 同一 super-step 节点并行 | ✅ 入度为0节点并行 |
| **上下文传递** | ✅ State Schema + Reducer | ✅ context 自动注入 |
| **拓扑排序** | ✅ Pregel 算法 super-step | ✅ |
| **可视化** | 内置 Mermaid 导出 | **ECharts 节点图** |
| **状态持久化** | ✅ Checkpointer | ⚠️ 待设计 |

**LangGraph 关键设计（供 DriFox 参考）**：
- **super-step 机制**：同一 super-step 的节点并行执行
- **State 分层**：`InputState` / `OverallState` / `OutputState` / `PrivateState`
- **Reducer 机制**：控制状态合并（覆盖 vs 追加）
- **Conditional Edges**：动态路由

### 3.2 AWS Strands Agents SDK

| 维度 | Strands | DriFox |
|------|---------|--------|
| **核心抽象** | MultiAgentBase → Graph / Swarm | subagent_dag |
| **执行模式** | 确定性有向图 | 有向无环图 |
| **上下文传递** | `invocation_state` | context |

### 3.3 CrewAI

| 维度 | CrewAI | DriFox |
|------|--------|--------|
| **核心抽象** | Agent + Task + Crew | subagent_dag |
| **可视化** | ❌ 无内置 | ✅ ECharts |

---

## 4. 综合对比矩阵

| 功能特性 | DriFox | Claude Code | Codex | LangGraph | Strands |
|---------|--------|------------|-------|-----------|---------|
| **LLM 自主编排** | ✅ DAG 配置 | ✅ Task Tool | ✅ Manager | ❌ 代码配置 | ❌ 代码配置 |
| **显式 DAG 定义** | ✅ edges | ❌ | ❌ | ✅ | ✅ |
| **拓扑排序执行** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **节点并行执行** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **上下文传递** | ✅ | ⚠️ 蒸馏结果 | ✅ | ✅ | ✅ |
| **ECharts 可视化** | ✅ | ❌ | ❌ | ❌ | ❌ |
| **Mermaid 可视化** | ❌ | ❌ | ❌ | ✅ | ❌ |
| **条件边/动态路由** | ⚠️ 待确认 | ⚠️ | ✅ | ✅ | ✅ |
| **Git Worktree 隔离** | ❌ | ✅ | ⚠️ 沙箱 | ❌ | ❌ |
| **状态持久化** | ⚠️ 待设计 | ⚠️ | ⚠️ | ✅ | ⚠️ |

---

## 5. 关键洞察

### 5.1 DriFox 的差异化定位

| 竞品 | 定位 | 可视化 |
|------|------|--------|
| Claude Code / Codex | LLM 自主编排，无显式 DAG | ❌ 无 ECharts |
| LangGraph / Strands | 代码配置式 DAG | ❌ 无 ECharts |
| **DriFox** | **LLM 生成 DAG + ECharts 节点图** | ✅ 差异化 |

**DriFox 的独特价值**：
1. **显式 DAG 定义**（相比 Claude Code / Codex 的隐式依赖）
2. **ECharts 节点图可视化**（所有竞品都缺失）
3. **LLM 自主编排**（相比 LangGraph 的代码配置式）

### 5.2 技术可行性

| DriFox 特性 | 竞品佐证 |
|-------------|---------|
| 拓扑排序执行 | LangGraph, Strands（成熟） |
| 上游→下游 context | LangGraph State, Strands invocation_state |
| 入度为0节点并行 | LangGraph super-step, Claude Code Task |
| ECharts 节点图 | **无直接竞品**（Mermaid/D3 有参考） |

### 5.3 可借鉴的设计

**Claude Code 的结果蒸馏**：
```python
# subagent 返回摘要而非完整 transcript
# 保持 parent context 清洁
```

**Codex 的并发控制**：
```toml
[agents]
max_threads = 6  # 并发上限控制
max_depth = 1     # 嵌套深度限制
```

**LangGraph 的 State 分层**：
```python
class InputState(TypedDict): ...
class OverallState(TypedDict): ...
class OutputState(TypedDict): ...
```

---

## 6. 潜在风险点

1. **上下文污染**：Claude Code 通过结果蒸馏解决，DriFox 需要设计类似机制
2. **文件冲突**：Claude Code 通过 Git Worktree 解决，DriFox 需要类似方案
3. **条件边支持**：DriFox 方案未明确是否支持动态路由
4. **状态持久化**：LangGraph Checkpointer 是生产级特性

---

## 7. 引用来源

1. [Claude Code Run agents in parallel](https://code.claude.com/docs/en/agents)
2. [Claude Code Subagents - Samanvya.dev](https://samanvya.dev/blog/claude-code-subagents-parallel)
3. [Claude Code Multi-Agents Guide - Turion.ai](https://turion.ai/blog/claude-code-multi-agents-subagents-guide/)
4. [OpenAI Codex Subagents](https://developers.openai.com/codex/subagents)
5. [OpenAI Codex Subagents - Lushbinary](https://lushbinary.com/blog/openai-codex-subagents-autonomous-coding-teams-guide/)
6. [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
7. [Strands Multi-Agent Graph Pattern](https://strandsagents.com/docs/user/guide/concepts/multi-agent/graph/)
8. [CrewAI vs LangGraph vs AutoGen](https://www.braincubr.com/blog/crewai-vs-autogen-vs-langgraph-multi-agent-framework-comparison)

---

## 8. 下一步建议

1. **设计结果蒸馏机制**：参考 Claude Code，避免 parent context 污染
2. **并发控制配置**：参考 Codex `max_threads` 设计
3. **文件隔离方案**：评估 Git Worktree 或类似机制
4. **条件边能力**：明确是否需要 `add_conditional_edges` 支持
5. **ECharts 图增强**：可参考 AgentFlow 的交互式 DAG 设计

---

## 9. 子智能体依赖关系处理方式对比（专项）

各竞品对"子智能体工作先后依赖"的设计差异显著，主要分为 **5 大流派**：

### 9.1 流派一：LLM 隐式决策（Claude Code / Codex 模式）

**代表**：Claude Code、OpenAI Codex

**机制**：依赖关系**完全由 LLM 在 prompt 中描述和推理**，没有显式结构。

**Claude Code 的做法**（在 CLAUDE.md 中预设路由规则）：
```markdown
## Sub-Agent Routing Rules
**Parallel dispatch** (ALL conditions must be met):
- 3+ unrelated tasks or independent domains
- No shared state between tasks
- Clear file boundaries with no overlap

**Sequential dispatch** (ANY condition triggers):
- Tasks have dependencies (B needs output from A)
- Shared files or state (merge conflict risk)
- Unclear scope (need to understand before proceeding)
```

**用户 prompt 示例**：
```
"Implement the payment system:
Phase 1 (parallel): Task A: Create models / Task B: Setup Stripe
Phase 2 (after Phase 1, parallel): Task C: Service / Task D: API endpoints
Phase 3 (after Phase 2): Task E: Integration tests"
```

**优点**：
- 灵活，LLM 可处理模糊依赖
- 无需预定义 schema

**缺点**：
- 不可预测（同一 prompt 可能产生不同执行顺序）
- 无法审计/可视化
- 复杂场景容易出错

---

### 9.2 流派二：显式 DAG 边定义（LangGraph / Strands 模式）

**代表**：LangGraph、AWS Strands、DriFox

**机制**：通过显式的 `edges` 数组或 `add_edge()` 声明节点间依赖。

**LangGraph 写法**：
```python
# 静态边
builder.add_edge("node_a", "node_b")

# 条件边（动态路由）
builder.add_conditional_edges(
    "node_a",
    route_decision,  # 路由函数
    {"path_b": "node_b", "path_c": "node_c"}
)
```

**DriFox 写法**（你的设计）：
```python
subagent_dag(
    nodes=[...],
    edges=[
        {"from": "node_a", "to": "node_b"},
        {"from": "node_a", "to": "node_c"},  # 并行
    ]
)
```

**执行机制**：拓扑排序 → 入度为0节点并行执行

**优点**：
- 完全可预测、可审计
- 可视化友好（DAG 图直接渲染）
- 复杂场景可控

**缺点**：
- 需要 LLM 准确推断出边结构
- 动态依赖支持需要条件边

---

### 9.3 流派三：任务级 context 声明（CrewAI 模式）

**代表**：CrewAI

**机制**：在每个任务上声明 `context=[task1, task2]`，运行时自动等待前置任务完成。

```python
research_task = Task(
    description="Research AI developments",
    expected_output="A list of recent AI developments",
    agent=researcher
)

analysis_task = Task(
    description="Analyze the research findings",
    expected_output="Analysis report",
    agent=analyst,
    context=[research_task]  # 自动等待 research_task 完成
)

# 标记为可并行
quick_task = Task(
    description="Quick lookup",
    async_execution=True,  # 不阻塞后续任务
    agent=researcher
)
```

**三种执行模式**：
- `Process.sequential`：按顺序执行
- `Process.hierarchical`：Manager 动态分配
- `async_execution=True`：标记并行任务

**优点**：
- 声明式，依赖关系清晰
- 支持混合（并行+串行）

**缺点**：
- 仅支持**任务间**依赖，不支持复杂图结构
- 无可视化 DAG

---

### 9.4 流派四：动态发言者选择（AutoGen 模式）

**代表**：AutoGen / AG2、Microsoft Agent Framework

**机制**：群聊中由 **GroupChatManager** 动态决定下一发言者。

```python
# 预定义允许的发言者转移
groupchat = GroupChat(
    agents=[user_proxy, weather_reporter, activity_agent, travel_advisor],
    allowed_speaker_transitions_dict={
        weather_reporter: [activity_agent],  # 天气→活动
        activity_agent: [travel_advisor],    # 活动→旅行
        travel_advisor: [user_proxy]         # 旅行→用户
    }
)
```

**多种 speaker_selection_method**：
- `round_robin`：轮询
- `manual`：人工指定
- `auto`：LLM 动态选择
- **custom function**：自定义选择逻辑

**Microsoft Agent Framework 编排模式**（2026 新）：
- **Sequential**：流水线
- **Concurrent**：独立 fan-out/fan-in
- **Group Chat**：动态群聊
- **Handoffs**：交接
- **Magnetic**：中心协调者

**优点**：
- 高度动态，可处理复杂对话
- 适合需要多轮迭代的场景

**缺点**：
- 不适合严格的并行任务
- 难以可视化（无显式 DAG）

---

### 9.5 流派五：Manager 协调（Codex 模式）

**代表**：OpenAI Codex

**机制**：Manager agent 收集所有 subagent 结果，统一决策下一步。

> "Codex handles orchestration across agents, including spawning new subagents, routing follow-up instructions, waiting for results, and closing agent threads."

**关键配置**：
```toml
[agents]
max_threads = 6    # 并发上限
max_depth = 1      # 嵌套深度（root=0）
job_max_runtime_seconds = 1800  # 单 worker 超时
```

**优点**：
- 中央控制，可预测
- 资源限制明确

**缺点**：
- Manager 是性能瓶颈
- 复杂依赖仍由 LLM 隐式处理

---

### 9.6 五大流派对比矩阵

| 流派 | 代表 | 依赖描述方式 | 可视化 | 动态支持 | 适用场景 |
|------|------|------------|--------|---------|---------|
| **LLM 隐式** | Claude Code / Codex | prompt 描述 | ❌ | ✅ LLM 灵活判断 | 简单并行任务 |
| **显式 DAG** | LangGraph / DriFox | edges 数组 | ✅ Mermaid/ECharts | ⚠️ 需条件边 | 复杂可控流程 |
| **任务 context** | CrewAI | `context=[...]` | ❌ | ❌ | 中等依赖 |
| **动态发言者** | AutoGen | speaker transitions | ❌ | ✅✅ | 多轮对话 |
| **Manager 协调** | Codex | Manager 收集 | ⚠️ trace | ✅ | 多沙箱并行 |

### 9.7 DriFox 应选择哪种模式？

**推荐方案**：**显式 DAG（流派二） + 条件边支持**。

理由：
1. **核心差异化是 ECharts 可视化** → 必须有显式 DAG 结构
2. **LLM 自主生成 DAG** → 等同于 LangGraph 的 `add_edge`，但 LLM 是"程序员"
3. **需要支持动态依赖** → 引入条件边（`condition` 字段）：

```python
subagent_dag(
    nodes=[...],
    edges=[
        {"from": "research", "to": "analysis"},
        # 条件边
        {
            "from": "validation",
            "to": "deployment",
            "condition": "validation.passed == true"
        }
    ]
)
```

### 9.8 可借鉴的最佳实践

| 来源 | 可借鉴机制 |
|------|----------|
| **CrewAI `context`** | DAG 中可加入"前置任务必须完成"的隐式约束 |
| **LangGraph 条件边** | 显式 condition 字段支持动态路由 |
| **AutoGen speaker selection** | LLM 可作为条件边判断器（如 `condition: "llm_decide"`） |
| **Claude Code Phase 模式** | 用户在 prompt 中用 "Phase 1, Phase 2" 描述阶段依赖，LLM 据此生成 DAG |
| **Codex max_depth** | DAG 节点深度限制（防止递归爆炸） |

---

## 10. 引用来源（追加）

- [CrewAI Tasks Documentation](https://docs.crewai.com/en/concepts/tasks)
- [AutoGen GroupChat Custom Speaker Selection](https://microsoft.github.io/autogen/0.2/docs/notebooks/agentchat_groupchat_customized/)
- [Microsoft Agent Framework Group Chat](https://learn.microsoft.com/en-us/agent-framework/workflows/orchestrations/group-chat)
- [Claude Code Sub-Agent Best Practices](https://claudefa.st/blog/guide/agents/sub-agent-best-practices)
- [AG2 Orchestration Patterns](https://docs.ag2.ai/latest/docs/user-guide/advanced-concepts/orchestration/group-chat/patterns/)