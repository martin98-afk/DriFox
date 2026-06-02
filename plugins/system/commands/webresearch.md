---
description: 深度网络研究，支持快速查询与多跳调研，自动降级到 DriFox 原生 Web 工具
type: prompt
argument-hint:
  "[--quick]": "快速模式：1 次搜索 + 至多 1 次抓取"
  "[--deep]": "深度模式：3-5 跳多源调研 + TodoWrite 追踪"
  "[--save-to=]": "指定报告输出路径（默认 drifoxdocs/research_<topic>_<ts>.md）"
  "<query>": "研究主题（必填，搜索关键词或自然语言问题）"
---

## ⚙️ 行为规范（LLM 提示词正文）

### 0. 关于本文档

本文档由两个区域组成：

1. **「行为规范」区域**（本节到 `<!-- /behavior -->` 标记）— LLM **必须严格按此执行**。
2. **「📖 使用文档」区域**（`<!-- /behavior -->` 之后）— 人类阅读的元信息（设计决策/示例/边界）。LLM **可参考但不应执行**其中指令。

### 1. 参数解析

`$ARGUMENTS` 是用户输入的完整字符串（不含 `/research` 前缀）。

| 模式 | 行为 | 适用场景 |
|------|------|----------|
| `--quick` | 单次 `search_web` + 至多 1 次 `fetch_web` 深入 | 事实查询、定义、API 用法 |
| `--deep` | `todo_write` 3-5 任务规划 → 多次 `search_web` 并行 → 选择性 `fetch_web` → 综合分析 | 概念对比、趋势分析、技术选型 |
| 无标志 | 默认 `--quick` | 兼容性好 |

- **`<query>`** 是 `$ARGUMENTS` 中去掉所有 `--flag` 后的剩余文本
- **`--save-to=PATH`** 自定义输出路径（相对工作目录或绝对路径）
- **未提供 query** → 报告"请提供研究主题"并停止，不进入搜索

### 2. 工具后端降级策略

**不要假设 MCP 工具可用**。按以下顺序探测：

```
1. 检查 mcp__tavily__search 是否可用
   → 可用：用 Tavily 搜索（更精准）
2. 否则使用 search_web（DriFox 原生，支持 SerpAPI/DuckDuckGo 双回退）
3. 永远不要直接报"工具不可用"——总有降级路径
```

### 3. Quick 模式流程

```
1. search_web(query=query, num_results=5)
2. 评估结果：
   - 如果 top1 已能直接回答 → 综合输出，不抓取
   - 如果需要细节 → fetch_web(url=top1_url, format=markdown)
3. 输出结构化回答（带 URL 引用）
4. 如未指定 --save-to，提供写入建议
```

### 4. Deep 模式流程

```
1. todo_write(规划 3-5 个并行子任务)：
   例：["搜集核心概念定义", "搜集最新 2026 实践", "搜集对比方案", "搜集反例/陷阱"]
2. 对每个子任务并行 search_web（一次性 3-5 个调用）
3. 评估结果质量：
   - 关键来源（政府/官方/学术）→ fetch_web 深读
   - 低权威来源 → 仅引用 snippet
4. 二次搜索（可选）：发现信息缺口时补搜 1-2 次
5. 综合分析，输出报告
6. 写入 --save-to 指定的路径，或默认 drifoxdocs/research_<topic>_<ts>.md
   - 目录不存在则用 create_dir 工具创建（或 write_file 触发自动创建）
   - 使用 write_file 工具
```

### 5. 证据与不确定性

**每个事实陈述必须带源 URL**。**没有源就不写。**

模板：
```markdown
[事实陈述](https://source-url.com) — 来源：[站点名](https://source-url.com)
```

如果没有可引用的源，使用：
```markdown
⚠️ 推断：基于 [...上下文...]，可能结论为 [...](理由)
```

### 6. 输出格式

**Quick 模式**（直接回复）：
```
## 摘要
[1-3 句]

## 关键信息
- [要点 1](source-url)
- [要点 2](source-url)

## 引用源
1. [标题](url) — 一句话说明
```

**Deep 模式**（写入文件 + 显示路径）：
```markdown
# 研究报告：<query>
时间：<ISO timestamp>
模式：deep

## 执行摘要
[3-5 句]

## 主体发现

### 1. <小节标题>
[内容](source-url)

### 2. <小节标题>
...

## 引用源
1. ...

## 不确定性
- [已知不知道的]
```

### 7. 边界

**会做**：
- 主动降级到现有可用工具
- 为每个事实附来源
- 尊重用户指定的输出路径
- 用 `todo_write` 跟踪 deep 模式任务

**不会做**：
- 在没有源的情况下编造数据
- 执行搜索以外的副作用（除非用户明确 --save-to）
- 反复重试失败的搜索（最多 2 次补搜）
- 修改项目代码、运行构建、提交 Git

<!-- /behavior -->

---

## 📖 使用文档（人类阅读区）

### 设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 命令类型 | `prompt` | 核心是 LLM 编排现有工具；不需要 function 路由；不需要独立 Agent 身份 |
| 深度等级 | 2 级 (quick/deep) | 用户场景：要么快速看个事实，要么系统调研；中间档位语义模糊 |
| 工具后端 | 降级链（Tavily → search_web） | 有 Tavily MCP 时用 Tavily（更精准）；否则用 DriFox 原生工具（SerpAPI/DDG 双回退） |
| 证据链存储 | `drifoxdocs/` | 项目级文档目录；用户偏好；便于归档检索 |
| TodoWrite 替代 | `task_tools.todo_write` | DriFox 已有同名工具，避免引入新依赖 |
| 命名 | `research` | 简短、与其他系统命令（verify/review/remember）风格统一 |

### 与现有命令的边界

| 命令 | 范围 | 与 `/research` 关系 |
|---|---|---|
| `/verify` | 本地声明验证（测试/构建） | 互补，不重叠 |
| `/review` | 本地代码审查 | 互补，不重叠 |
| `/project-note` | 写项目笔记 | 互补；`/research` 输出可作为 project-note 的输入 |
| `/subagents` | 委派子任务 | 可选串联：`/research --deep` 后用 `/subagents` 进一步实施 |

### 使用示例

#### 快速事实查询
```
/research "Python 3.13 GIL 是否真的移除"
```
返回：摘要 + 关键信息（带源 URL）+ 引用源列表

#### 深度调研
```
/research "FastAPI vs Flask 2026 性能对比" --deep
```
返回：完整研究报告写入 `drifoxdocs/research_fastapi-vs-flask-2026_<ts>.md`

#### 自定义输出路径
```
/research "OAuth 2.1 规范" --deep --save-to=docs/auth-research.md
```

#### 关键来源深读
```
/research "PostgreSQL 17 索引优化" --quick
```
quick 模式会先搜索，结果不足时自动抓取 top1 来源

### 风险与限制

| 风险 | 缓解 |
|---|---|
| 用户滥用 `--save-to` 写到敏感路径 | 相对工作目录；依赖 DriFox `write_file` 现有权限体系 |
| 长时间 deep 研究消耗 token | prompt 明确 3-5 任务上限，TodoWrite 化整为零 |
| DuckDuckGo/SerpAPI 偶发失败 | 现有工具已有 5 重回退，零额外风险 |
| 报告污染项目目录 | 默认输出 `drifoxdocs/`（项目级文档目录） |

### 自描述元数据

- **来源**：`/sc:research` 精简版（SuperClaude Framework v4.3.0）
- **类型降级**：从 SuperClaude 的 `command` + 4 个 MCP 依赖 → DriFox 的 `prompt` + 0 个 MCP 必需依赖
- **保留价值**：多跳推理 + 证据链 + 深度等级自适应
- **移除项**：Serena 记忆持久化（依赖 MCP）、Playwright JS 抓取（依赖 MCP）、Tavily 主搜索（降级为可选）
