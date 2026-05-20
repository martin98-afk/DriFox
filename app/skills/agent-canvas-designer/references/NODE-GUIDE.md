# 节点选型指南

## 节点类型速查（14 种）

| type | 用途 | 选型判断 |
|------|------|---------|
| `start` | 流程起点 | 每个流程必有 |
| `end` | 流程终点 | 每个流程必有 |
| `llm` | 单次大模型调用 | 单轮推理/分析/总结/翻译/评价/改写 |
| `agent` | 带工具多轮Agent | **需要工具调用或多步推理迭代时用** |
| `knowledge` | 知识库检索 | 查文档/FAQ/知识库，不是智能体 |
| `ask-table` | NL→SQL查表 | 查数据库/统计/结构化数据 |
| `api-request` | HTTP接口调用 | 调外部API获取实时数据 |
| `reply` | 输出回复 | 把最终结果返回给用户 |
| `condition` | 条件分支 | 按条件走不同后续路径 |
| `classifier` | 问题分类 | 输入→分类标签（路由用） |
| `code` | Python代码 | 计算/转换/数据清洗 |
| `param-extractor` | 结构化提取 | 从文本提取字段→JSON |
| `doc-extractor` | 文件提取 | 提取文档/pdf/图片内容 |
| `container` | 子画布容器 | 循环/迭代/子流程分组 |

---

## Agent vs LLM 抉择

| 选 `agent` | 选 `llm` |
|-----------|---------|
| 需要调工具（检索/API/代码） | 纯文本推理 |
| 多步推理迭代 | 单次输入→输出 |
| 需要结构化JSON输出 | 自由文本回答 |
| 自主决策下一步动作 | 被动按指令处理 |

---

## 决策树

```
入口输入
    ↓
需要分类路由？ → 是 → classifier
    ↓ 否
需要查数据库？ → 是 → ask-table
    ↓ 否
需要查知识库？ → 是 → knowledge
    ↓ 否
需要调外部API？ → 是 → api-request
    ↓ 否
需要多轮迭代/工具调用？ → 是 → agent
    ↓ 否
单次推理？ → 是 → llm
```

---

## 常见设计模式

### 模式 A：路由分发型
```
开始 → 分类器 → 分支A(LLM) / 分支B(Agent) / 分支C(知识检索) → 回复 → 结束
```
适用：多类型用户请求分流处理

### 模式 B：RAG 增强型
```
开始 → 知识检索 → LLM(带上下文) → 回复 → 结束
```
适用：需要领域知识支撑的问答

### 模式 C：多步流水线型
```
开始 → LLM(分析) → Agent(执行) → Code(计算) → LLM(总结) → 回复 → 结束
```
适用：复杂分析报告生成

### 模式 D：人在回路型
```
开始 → Agent(初稿) → 容器(对话调整) → LLM(终审) → 回复 → 结束
```
适用：需要用户确认的生成任务

### 模式 E：多源融合型
```
开始 → 知识检索 ─┐
         API请求  ─┤→ Agent(综合决策) → 回复 → 结束
         数据库查询─┘
```
适用：需要融合多数据源的决策场景

---

## 场景对应表

| 业务场景 | 推荐节点组合 |
|---------|-------------|
| 客服工单路由 | start → classifier → [分支xN] → reply → end |
| 合同审查 | start → doc-extractor → llm → reply → end |
| 数据分析报告 | start → llm → agent → code → llm → reply → end |
| 代码审查流水线 | start → classifier → llm → code → reply → end |
| 内容创作 | start → llm → knowledge → llm → reply → end |
| 研究助手 | start → llm → knowledge → ask-table → llm → reply → end |
| 运维诊断 | start → llm → agent → knowledge → reply → end |
| HR简历筛选 | start → classifier → llm → reply → end |
| 教育评估 | start → llm → code → llm → reply → end |