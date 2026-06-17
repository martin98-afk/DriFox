---
description: 使用 subagent_dag 将复杂任务拆解为 DAG 工作流并自动执行
type: prompt
argument-hint:
  "[--pipeline]": "流水线模式：串行依赖链，前一个节点输出作为后一个输入"
  "[--fanout]": "扇出模式：先并行探索/研究，再汇聚分析"
  "[--hybrid]": "混合模式：含分支和汇合点的复杂依赖（默认）"
  "[--nodes=]": "节点数建议：建议拆分为几个子任务，默认自动判断"
  "<task-desc>": "任务描述（必填），描述你想要完成的复杂任务"
mutex_groups:
  mode: ["--pipeline", "--fanout", "--hybrid"]
prompt_sections:
  --pipeline: "pipeline"
  --fanout: "fanout"
  --hybrid: "hybrid"
---

## ⚙️ 行为规范（LLM 提示词正文）

### 1. 核心目标

你收到一个复杂任务 `$ARGUMENTS`。你的目标不是直接做这个任务，而是：
1. **分析任务结构**，判断哪些部分可以并行、哪些有依赖关系
2. **设计 DAG 工作流**，明确节点职责和边依赖
3. **使用 `subagent_dag` 工具执行**，让子智能体并行工作
4. **汇总最终结果**，给用户清晰的完成报告

### 2. 参数解析

`$ARGUMENTS` 是用户输入的完整字符串（不含 `/dag` 前缀）。

| 模式 | 行为 | 适用场景 |
|------|------|----------|
| `--pipeline` | 流水线模式：串行依赖链 | 有明确前后依赖的多阶段任务 |
| `--fanout` | 扇出模式：先并行探索再汇聚分析 | 需多角度收集信息再统一分析 |
| `--hybrid` | 混合模式：分支+汇合（默认） | 大多数复杂任务 |
| `--nodes=N` | 建议拆分为 N 个子任务 | 用户对粒度有明确要求时 |
| 无参数 | 默认 `--hybrid`，自动判断粒度 | 兼容性好 |

- **`<task-desc>`** 是 `$ARGUMENTS` 中去掉所有 `--flag` 后的剩余文本

### 3. DAG 分解思维框架

#### 3.1 通用分解步骤

```
步骤 1: 理解任务 → 识别出 3-7 个可独立分配的子任务
步骤 2: 标记依赖 → 哪些子任务需要等另一些先完成？
步骤 3: 设计拓扑 → 没有依赖的可以并行，有依赖的串成链
步骤 4: 分配智能体 → 从 Available Subagents 中选最合适的
步骤 5: 执行 → 调用 subagent_dag(nodes, edges)
步骤 6: 汇总 → 优先只读叶子节点结果，满意即止不上查中间节点
```

### 4. DAG 设计原则

#### 4.1 节点粒度

| 粒度 | 节点数 | 适用场景 |
|------|--------|----------|
| 粗粒度 | 2-3 | 任务边界清晰，每个节点工作量较大 |
| 中粒度 | 4-6 | 大多数复杂任务（推荐） |
| 细粒度 | 7-10 | 大型多步骤任务，需精细控制 |

- 每个节点应该是逻辑上完整的子任务
- 避免过度拆分（1-2 次工具调用就完成 → 太细）
- 避免拆分不足（多个互相独立的事挤在一个节点 → 太粗）
- 节点描述要包含完整上下文：目标、已知信息、输出格式

#### 4.2 依赖与并行

- 没有依赖关系的节点**必须并行**——这是 DAG 的核心价值
- 有依赖的节点通过 `edges` 声明，系统自动等上游完成再启动下游
- 下游节点的 context 中自动注入上游结果，因此上游输出要结构清晰

#### 4.3 智能体选择

| 任务类型 | 推荐 agent |
|----------|-----------|
| 信息收集、只读分析 | 描述含"代码探索"或"只读分析"的 agent |
| 读写改的实现任务 | 有写/编辑权限的通用 agent |
| 代码审查、质量检查 | 描述含"审查"或"检查"的 agent |
| 架构设计、方案规划 | 有分析能力的 agent |

### 5. 执行流程

```
1. 分析任务 → 设计 DAG 结构（nodes + edges）
2. 简单设计（≤3 节点）：直接执行。复杂设计（4+ 节点）：先描述 DAG 让用户看一眼
3. subagent_dag({ nodes: [...], edges: [...] })
4. 结果选择性消费：优先只读叶子节点（edges 中没有作为 from 出现的节点 ID）
   → 叶子节点结果满意 → 直接汇总输出
   → 不满意 → 追溯上游补充读取
5. 综合分析，给出完整报告
```

⚠️ **不要一次性 subagent_status 查全部节点日志**。下游节点已汇聚上游精华。

### 6. 硬性规则：所有节点必须在同一个 DAG 中

🚨 **无论任务多复杂，只调用一次 `subagent_dag`。**

错误做法（❌ 多层拆成多个 DAG）：
```
subagent_dag({ nodes: [设计] })            ← 第一次
subagent_dag({ nodes: [实现A, 实现B] })    ← 第二次（错误！）
```

正确做法（✅ 一次调完）：
```
subagent_dag({
  nodes: [设计, 实现A, 实现B, 测试A, 测试B, 集成验证],
  edges: [{ from: "设计", to: "实现A" }, ...]
})
```

系统自动按拓扑排序分批并行执行，你不需要分批调用。

### 7. 节点 description 编写要点

```
好的 description：
  "在 src/auth/ 下分析登录流程，找出所有 API 端点定义，
   输出：每行 method, path, 中间件列表"

坏的 description：
  "分析认证模块"
```

**必须包含**：工作范围、具体要做什么、输出格式要求。

### 8. 边界

**会做**：分析任务结构设计 DAG、选择合适 agent、注入上下文、汇总结果
**不会做**：不分析直接丢给一个 subagent、隐含依赖不声明 edges、可并行不并行

### 9. 错误恢复

- 优先看叶子节点结果——即使上游失败，叶子有可用结果就直接用
- 受影响则追溯失败的上游，用 `subagent_status` 查看详情
- 判断是否可重试（修复描述后重新派发）
- skipped 的下游节点不需要手动处理

<!-- section:pipeline -->
### 10. 示例：流水线模式（--pipeline）

```
nodes:
  - id: "design"
    agent: "<代码探索>"
    description: "分析现有 schema 和模型，输出需修改的文件清单"
  - id: "implement"
    agent: "<通用>"
    description: "基于 design 输出，实现功能（路由、验证、数据库）"
  - id: "test"
    agent: "<通用>"
    description: "编写测试用例，覆盖正常流程和边界"

edges:
  - from: "design"     to: "implement"
  - from: "implement"  to: "test"
```
<!-- end -->

<!-- section:fanout -->
### 10. 示例：扇出模式（--fanout）

```
nodes:
  - id: "explore_auth"
    agent: "<代码探索>"
    description: "探索 src/auth/，列出文件结构和关键函数"
  - id: "explore_payment"
    agent: "<代码探索>"
    description: "探索 src/payment/，列出 API 端点和调用链"
  - id: "summary"
    agent: "<综合分析>"
    description: "综合 auth 和 payment 结果，输出架构对比"

edges:
  - from: "explore_auth"    to: "summary"
  - from: "explore_payment" to: "summary"
```
<!-- end -->

<!-- section:hybrid -->
### 10. 示例：混合模式（--hybrid）

```
nodes:
  - id: "analyze"
    agent: "<代码探索>"
    description: "分析现有 v1 路由定义..."
  - id: "migrate_users"
    agent: "<通用>"
    description: "迁移用户相关路由..."
  - id: "migrate_orders"
    agent: "<通用>"
    description: "迁移订单相关路由..."
  - id: "migrate_payments"
    agent: "<通用>"
    description: "迁移支付相关路由..."
  - id: "verify"
    agent: "<审查>"
    description: "统一验证所有迁移后的路由..."

edges:
  - from: "analyze"          to: "migrate_users"
  - from: "analyze"          to: "migrate_orders"
  - from: "analyze"          to: "migrate_payments"
  - from: "migrate_users"    to: "verify"
  - from: "migrate_orders"   to: "verify"
  - from: "migrate_payments" to: "verify"
```
<!-- end -->
