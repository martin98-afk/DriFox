---
description: 深度网络研究，支持快速查询与多跳调研，自动降级到 DriFox 原生 Web 工具
type: prompt
argument-hint:
  "[--quick]": "快速模式：1 次搜索 + 至多 1 次抓取"
  "[--deep]": "深度模式：3-5 跳多源调研 + TodoWrite 追踪"
  "[--html]": "HTML 报告模式：输出为带统一样式的 HTML 报告（配合 --save-to 使用）"
  "[--save-to=]": "指定报告输出路径（默认 drifoxdocs/research_<topic>_<ts>.md，--html 时扩展名为 .html）"
  "<query>": "研究主题（必填，搜索关键词或自然语言问题）"
---

## ⚙️ 行为规范（LLM 提示词正文）

### 1. 参数解析

`$ARGUMENTS$` 是用户输入的完整字符串（不含 `/research` 前缀）。

| 模式 | 行为 | 适用场景 |
|------|------|----------|
| `--quick` | 单次 `search_web` + 至多 1 次 `fetch_web` 深入 | 事实查询、定义、API 用法 |
| `--deep` | `todo_write` 3-5 任务规划 → 多次 `search_web` 并行 → 选择性 `fetch_web` → 综合分析 | 概念对比、趋势分析、技术选型 |
| 无标志 | 默认 `--quick` | 兼容性好 |

- **`<query>`** 是 `$ARGUMENTS` 中去掉所有 `--flag` 后的剩余文本
- **`--save-to=PATH`** 自定义输出路径（相对工作目录或绝对路径）
- **`--html`** 开启 HTML 报告模式，与 `--deep` 或 `--quick` 均可搭配；输出时将使用下方定义的统一 HTML 风格渲染报告
- **未提供 query** → 报告"请提供研究主题"并停止，不进入搜索

### 2. 统一 HTML 报告风格定义

当 `--html` 参数启用时，报告输出必须使用以下 HTML 模板渲染。保证所有生成结果风格统一。

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{TITLE} — DriFox 研究报告</title>
<style>
/* ===== 基础重置 ===== */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 16px; -webkit-font-smoothing: antialiased; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
  color: #1a1a2e;
  background: #f5f6fa;
  line-height: 1.7;
  padding: 2rem 1rem;
}

/* ===== 报告容器 ===== */
.report {
  max-width: 900px;
  margin: 0 auto;
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 4px 24px rgba(0,0,0,0.08);
  overflow: hidden;
}

/* ===== 页眉 ===== */
.report-header {
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
  color: #fff;
  padding: 2.5rem 3rem;
}
.report-header h1 {
  font-size: 1.8rem;
  font-weight: 700;
  margin-bottom: 0.5rem;
  line-height: 1.3;
}
.report-header .meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem 1.5rem;
  font-size: 0.85rem;
  opacity: 0.85;
  margin-top: 0.75rem;
}
.report-header .meta span { display: inline-flex; align-items: center; gap: 0.35rem; }
.report-header .meta .badge {
  display: inline-block;
  padding: 0.15rem 0.6rem;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 600;
  background: rgba(255,255,255,0.15);
}

/* ===== 正文 ===== */
.report-body { padding: 2rem 3rem 3rem; }

/* ===== 摘要 / 执行摘要 ===== */
.exec-summary {
  background: #f0f4ff;
  border-left: 4px solid #0f3460;
  padding: 1.25rem 1.5rem;
  border-radius: 0 8px 8px 0;
  margin-bottom: 2rem;
}
.exec-summary p { margin: 0; color: #2d3748; }

/* ===== 标题层级 ===== */
.report-body h2 {
  font-size: 1.35rem;
  font-weight: 700;
  color: #1a1a2e;
  margin: 2rem 0 1rem;
  padding-bottom: 0.4rem;
  border-bottom: 2px solid #e2e8f0;
}
.report-body h3 {
  font-size: 1.1rem;
  font-weight: 600;
  color: #2d3748;
  margin: 1.5rem 0 0.75rem;
}

/* ===== 段落与文本 ===== */
.report-body p { margin: 0 0 1rem; color: #4a5568; }
.report-body p:last-child { margin-bottom: 0; }

/* ===== 链接 ===== */
.report-body a {
  color: #2563eb;
  text-decoration: none;
  border-bottom: 1px solid transparent;
  transition: border-color 0.15s;
}
.report-body a:hover { border-bottom-color: #2563eb; }

/* ===== 引用来源标记 ===== */
.source-tag {
  display: inline-block;
  font-size: 0.75rem;
  font-weight: 500;
  color: #2563eb;
  background: #eff6ff;
  padding: 0.1rem 0.45rem;
  border-radius: 4px;
  margin-left: 0.15rem;
  cursor: help;
  vertical-align: super;
  line-height: 1.2;
}

/* ===== 推断 / 不确定性标注 ===== */
.callout {
  margin: 1rem 0;
  padding: 1rem 1.25rem;
  border-radius: 8px;
  font-size: 0.9rem;
}
.callout-infer {
  background: #fffbeb;
  border-left: 4px solid #f59e0b;
  color: #92400e;
}
.callout-uncertainty {
  background: #fef2f2;
  border-left: 4px solid #ef4444;
  color: #991b1b;
}
.callout-info {
  background: #ecfdf5;
  border-left: 4px solid #10b981;
  color: #065f46;
}

/* ===== 引用源列表 ===== */
.ref-list { list-style: none; padding: 0; }
.ref-list li {
  padding: 0.5rem 0;
  border-bottom: 1px solid #f0f0f0;
  font-size: 0.9rem;
  color: #4a5568;
}
.ref-list li:last-child { border-bottom: none; }
.ref-list .ref-index {
  display: inline-block;
  width: 1.5rem;
  height: 1.5rem;
  line-height: 1.5rem;
  text-align: center;
  background: #e2e8f0;
  border-radius: 4px;
  font-size: 0.75rem;
  font-weight: 600;
  margin-right: 0.5rem;
  color: #1a1a2e;
}

/* ===== Todo 追踪（Deep 模式时使用） ===== */
.todo-track {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 1rem 1.25rem;
  margin-bottom: 1.5rem;
}
.todo-track h4 { font-size: 0.85rem; color: #64748b; margin-bottom: 0.5rem; text-transform: uppercase; letter-spacing: 0.5px; }
.todo-track ul { list-style: none; padding: 0; display: flex; flex-wrap: wrap; gap: 0.4rem; }
.todo-track li {
  display: inline-flex; align-items: center; gap: 0.3rem;
  font-size: 0.8rem;
  padding: 0.2rem 0.6rem;
  border-radius: 999px;
  background: #e2e8f0;
  color: #475569;
}
.todo-track .done { background: #d1fae5; color: #065f46; text-decoration: line-through; }

/* ===== 页脚 ===== */
.report-footer {
  padding: 1.25rem 3rem;
  border-top: 1px solid #e2e8f0;
  font-size: 0.8rem;
  color: #94a3b8;
  text-align: center;
}

/* ===== 响应式 ===== */
@media (max-width: 640px) {
  body { padding: 0.5rem; }
  .report-header { padding: 1.5rem 1.25rem; }
  .report-header h1 { font-size: 1.35rem; }
  .report-body { padding: 1.25rem 1.25rem 2rem; }
  .report-footer { padding: 1rem 1.25rem; }
}
</style>
</head>
<body>
<div class="report">
  <div class="report-header">
    <h1>{TITLE}</h1>
    <div class="meta">
      <span>📅 {TIMESTAMP}</span>
      <span>⚙️ {MODE}</span>
      <span class="badge">DriFox Research</span>
    </div>
  </div>
  <div class="report-body">
    {CONTENT}
  </div>
  <div class="report-footer">
    由 DriFox 深度研究报告引擎生成 · 事实以引用源为准
  </div>
</div>
</body>
</html>
```

**使用说明**：

| 占位符 | 替换为 |
|--------|--------|
| `{TITLE}` | 报告标题（如 `研究报告：量子计算》`） |
| `{TIMESTAMP}` | ISO 时间戳（如 `2026-06-03T15:44:40+08:00`） |
| `{MODE}` | 模式标签（`Quick` 或 `Deep`） |
| `{CONTENT}` | HTML 正文（见下方各模式的 HTML 输出格式） |

**正文 HTML 转换规则**（将 Markdown 结构映射为 HTML）：
- `## 摘要` → `<h2>摘要</h2>`
- `## 关键信息` → `<h2>关键信息</h2>`，列表项保持 `<ul>/<li>`
- `## 执行摘要` → `<div class="exec-summary"><p>...</p></div>`
- `## 主体发现` → `<h2>主体发现</h2>`
- `### 小标题` → `<h3>小标题</h3>`
- `## 引用源` → `<h2>引用源</h2>`，列表使用 `<ul class="ref-list"><li><span class="ref-index">N</span> ...</li></ul>`
- `## 不确定性` → `<div class="callout callout-uncertainty"><strong>⚠️ 不确定性</strong><p>...</p></div>`
- `⚠️ 推断` → `<div class="callout callout-infer"><strong>🔮 推断</strong><p>...</p></div>`
- 普通 `[事实](url)` → `<a href="url">事实</a>` 紧跟 `<sup class="source-tag">源</sup>`
- Deep 模式的 todo 追踪 → `<div class="todo-track">...</div>`

### 3. 工具后端降级策略

**不要假设 MCP 工具可用**。按以下顺序探测：

```
1. 检查 mcp__tavily__search 是否可用
   → 可用：用 Tavily 搜索（更精准）
2. 否则使用 search_web（DriFox 原生，支持 SerpAPI/DuckDuckGo 双回退）
3. 永远不要直接报"工具不可用"——总有降级路径
```

### 4. Quick 模式流程

```
1. search_web(query=query, num_results=5)
2. 评估结果：
   - 如果 top1 已能直接回答 → 综合输出，不抓取
   - 如果需要细节 → fetch_web(url=top1_url, format=markdown)
3. 输出结构化回答（带 URL 引用）
   - 如指定 --html，使用下方 HTML 模板渲染输出（取代 Markdown 回复）
   - 否则输出 Markdown 格式
4. 如未指定 --save-to，提供写入建议
```

### 5. Deep 模式流程

```
1. todo_write(规划 3-5 个并行子任务)：
   例：["搜集核心概念定义", "搜集最新 2026 实践", "搜集对比方案", "搜集反例/陷阱"]
2. 对每个子任务并行 search_web（一次性 3-5 个调用）
3. 评估结果质量：
   - 关键来源（政府/官方/学术）→ fetch_web 深读
   - 低权威来源 → 仅引用 snippet
4. 二次搜索（可选）：发现信息缺口时补搜 1-2 次
5. 综合分析，输出报告
6. 写入 --save-to 指定的路径，或默认路径
   - 目录不存在则用 create_dir 工具创建（或 write_file 触发自动创建）
   - 使用 write_file 工具
   - 指定 --html 时，文件扩展名为 .html，内容使用下方 HTML 模板渲染
   - 未指定 --html 时，文件扩展名为 .md，输出 Markdown 格式
```

### 6. 证据与不确定性

**每个事实陈述必须带源 URL**。**没有源就不写。**

模板：
```markdown
[事实陈述](https://source-url.com) — 来源：[站点名](https://source-url.com)
```

如果没有可引用的源，使用：
```markdown
⚠️ 推断：基于 [...上下文...]，可能结论为 [...](理由)
```

### 7. 输出格式

**Quick 模式**（直接回复）：

无 `--html` 时输出 Markdown：
```
## 摘要
[1-3 句]

## 关键信息
- [要点 1](source-url)
- [要点 2](source-url)

## 引用源
1. [标题](url) — 一句话说明
```

`--html` 时输出渲染后的 HTML（直接在对话中返回，对话工具会自动渲染预览）：
```
<!-- 实际返回 HTML 包裹在 ```html 代码块中供渲染，或直接输出 HTML 文本 -->
```
HTML 内容遵循统一风格模板：
- `<h2>摘要</h2>` + 段落
- `<h2>关键信息</h2>` + `<ul><li>` 列表，每个要点旁带 `<sup class="source-tag">源</sup>`
- `<h2>引用源</h2>` + `<ul class="ref-list">`

**Deep 模式**（写入文件 + 显示路径）：

无 `--html` 时输出 Markdown 文件：
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

`--html` 时输出 HTML 文件，内容遵循统一风格模板：
- 标题区使用 `<div class="report-header">`，含标题、时间戳、模式标签
- `## 执行摘要` → `<div class="exec-summary"><p>...</p></div>`
- `## 主体发现` → `<h2>主体发现</h2>`，子节使用 `<h3>`
- 每个 `[事实](url)` → `<a href="url">事实</a><sup class="source-tag">源</sup>`
- `## 引用源` → `<h2>引用源</h2>` + `<ul class="ref-list">`
- `## 不确定性` → `<div class="callout callout-uncertainty">`
- `⚠️ 推断` → `<div class="callout callout-infer">`
- Todo 追踪信息 → `<div class="todo-track">`（如有）

### 8. 边界

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