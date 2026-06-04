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

`$ARGUMENTS` 是用户输入的完整字符串（不含 `/research` 前缀）。

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
/* ===========================================
   DriFox Research Report — Modern Style
   风格定位：Premium Minimal · 高端极简
   =========================================== */

/* ===== 基础重置 ===== */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-size:16px;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;text-rendering:optimizeLegibility}
body{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei","Noto Sans SC",sans-serif;
  color:#0a0a14;
  background:#f6f6f4;
  line-height:1.7;
  padding:3rem 1.25rem;
  font-feature-settings:"ss01","cv11";
}

/* ===== 报告容器 ===== */
.report{
  max-width:920px;margin:0 auto;
  background:#fff;
  border-radius:20px;
  box-shadow:
    0 1px 2px rgba(10,10,20,.04),
    0 24px 48px -12px rgba(10,10,20,.12),
    0 12px 24px -8px rgba(10,10,20,.06);
  overflow:hidden;
  border:1px solid #ececec;
}

/* ===== 页眉（封面区） ===== */
.report-header{
  position:relative;
  background:
    radial-gradient(at 18% 8%, rgba(99,102,241,.32) 0px, transparent 50%),
    radial-gradient(at 82% 92%, rgba(236,72,153,.22) 0px, transparent 50%),
    linear-gradient(135deg,#0a0a14 0%,#15152a 50%,#1c1c38 100%);
  color:#fff;
  padding:3.5rem 3rem 3rem;
  overflow:hidden;
}
.report-header::before{
  content:"";position:absolute;inset:0;
  background-image:
    linear-gradient(rgba(255,255,255,.05) 1px,transparent 1px),
    linear-gradient(90deg,rgba(255,255,255,.05) 1px,transparent 1px);
  background-size:32px 32px;
  -webkit-mask-image:radial-gradient(ellipse 80% 60% at 50% 30%,#000 0%,transparent 70%);
  mask-image:radial-gradient(ellipse 80% 60% at 50% 30%,#000 0%,transparent 70%);
  pointer-events:none;
}
.report-header > *{position:relative;z-index:1}
.report-header .eyebrow{
  display:inline-flex;align-items:center;gap:.55rem;
  font-size:.72rem;font-weight:600;letter-spacing:.18em;
  text-transform:uppercase;opacity:.72;
  margin-bottom:1.1rem;
}
.report-header .eyebrow::before{
  content:"";width:24px;height:1px;background:currentColor;
}
.report-header h1{
  font-family:Georgia,"Noto Serif SC","Source Han Serif SC","Songti SC",serif;
  font-size:2.4rem;font-weight:600;
  line-height:1.22;letter-spacing:-.02em;
  text-wrap:balance;
  margin-bottom:1.4rem;
  max-width:36ch;
}
.report-header .meta{
  display:flex;flex-wrap:wrap;gap:.5rem;
  font-size:.82rem;
}
.report-header .meta span{
  display:inline-flex;align-items:center;gap:.4rem;
  padding:.38rem .85rem;
  background:rgba(255,255,255,.08);
  border:1px solid rgba(255,255,255,.14);
  border-radius:999px;
  font-weight:500;
  backdrop-filter:blur(8px);
  -webkit-backdrop-filter:blur(8px);
}
.report-header .meta .badge{
  background:rgba(255,255,255,.16);
  border-color:rgba(255,255,255,.28);
  font-weight:600;
  letter-spacing:.04em;
}

/* ===== 正文 ===== */
.report-body{padding:3rem 3.5rem 3.5rem}

/* ===== 执行摘要 ===== */
.exec-summary{
  position:relative;
  background:linear-gradient(135deg,#f5f3ff 0%,#fdf4ff 100%);
  border:1px solid #ede9fe;
  padding:1.5rem 1.75rem;
  border-radius:12px;
  margin-bottom:2.75rem;
  font-size:1.05rem;line-height:1.72;
  overflow:hidden;
}
.exec-summary::before{
  content:"";position:absolute;left:0;top:0;bottom:0;
  width:3px;
  background:linear-gradient(135deg,#6366f1 0%,#8b5cf6 50%,#ec4899 100%);
}
.exec-summary p{margin:0;color:#374151}
.exec-summary p + p{margin-top:.7rem}

/* ===== 标题层级 ===== */
.report-body h2{
  font-family:Georgia,"Noto Serif SC","Source Han Serif SC","Songti SC",serif;
  font-size:1.7rem;font-weight:600;
  color:#0a0a14;
  margin:2.75rem 0 1.1rem;
  padding-bottom:.75rem;
  letter-spacing:-.01em;
  position:relative;
}
.report-body h2::after{
  content:"";position:absolute;left:0;bottom:0;
  width:36px;height:2px;
  background:linear-gradient(90deg,#6366f1 0%,#8b5cf6 50%,#ec4899 100%);
  border-radius:2px;
}
.report-body h2:first-child{margin-top:0}
.report-body h3{
  font-size:1.15rem;font-weight:600;
  color:#0a0a14;
  margin:2rem 0 .8rem;
  letter-spacing:-.005em;
  display:flex;align-items:center;gap:.65rem;
}
.report-body h3::before{
  content:"";flex:none;
  width:7px;height:7px;
  background:linear-gradient(135deg,#6366f1,#8b5cf6);
  border-radius:2px;
  transform:rotate(45deg);
}

/* ===== 段落与文本 ===== */
.report-body p{margin:0 0 1.1rem;color:#40414a;font-size:1rem}
.report-body p:last-child{margin-bottom:0}
.report-body strong{color:#0a0a14;font-weight:600}

/* ===== 链接 ===== */
.report-body a{
  color:#6366f1;text-decoration:none;
  background-image:linear-gradient(currentColor,currentColor);
  background-size:100% 1px;background-repeat:no-repeat;background-position:0 100%;
  transition:color .15s;
}
.report-body a:hover{color:#8b5cf6}

/* ===== 引用源标记 ===== */
.source-tag{
  display:inline-block;
  font-size:.65rem;font-weight:700;
  color:#fff;
  background:linear-gradient(135deg,#6366f1 0%,#8b5cf6 50%,#ec4899 100%);
  padding:.1rem .42rem;
  border-radius:4px;
  margin-left:.2rem;
  vertical-align:super;line-height:1.3;
  letter-spacing:.04em;text-transform:uppercase;
  font-variant-numeric:tabular-nums;
}

/* ===== 推断 / 不确定性 / 信息 标注 ===== */
.callout{
  margin:1.5rem 0;
  padding:1.1rem 1.4rem;
  border-radius:10px;
  font-size:.93rem;line-height:1.65;
  border:1px solid;
  position:relative;
}
.callout strong{display:block;font-weight:600;margin-bottom:.4rem;font-size:.85rem;letter-spacing:.02em}
.callout p{margin:0}
.callout p + p{margin-top:.5rem}
.callout-infer{background:#fffbeb;border-color:#fde68a;color:#78350f}
.callout-uncertainty{background:#fef2f2;border-color:#fecaca;color:#7f1d1d}
.callout-info{background:#ecfdf5;border-color:#a7f3d0;color:#064e3b}

/* ===== 引用源列表 ===== */
.ref-list{list-style:none;padding:0;display:grid;gap:.6rem}
.ref-list li{
  position:relative;
  padding:.9rem 1rem .9rem 3.4rem;
  background:#fff;
  border:1px solid #ececec;
  border-radius:10px;
  font-size:.92rem;color:#40414a;
  transition:border-color .2s,box-shadow .2s,transform .2s;
}
.ref-list li:hover{
  border-color:#c7d2fe;
  box-shadow:0 1px 2px rgba(10,10,20,.04),0 6px 16px rgba(99,102,241,.10);
  transform:translateY(-1px);
}
.ref-list .ref-index{
  position:absolute;left:.85rem;top:50%;
  transform:translateY(-50%);
  display:inline-flex;align-items:center;justify-content:center;
  width:1.85rem;height:1.85rem;
  background:linear-gradient(135deg,#6366f1 0%,#8b5cf6 50%,#ec4899 100%);
  color:#fff;
  border-radius:7px;
  font-size:.75rem;font-weight:700;
  font-variant-numeric:tabular-nums;
  box-shadow:0 2px 6px rgba(99,102,241,.25);
}

/* ===== Todo 追踪（Deep 模式时使用） ===== */
.todo-track{
  background:linear-gradient(135deg,#f8fafc 0%,#f1f5f9 100%);
  border:1px solid #e2e8f0;
  border-radius:10px;
  padding:1.1rem 1.4rem;
  margin-bottom:2rem;
}
.todo-track h4{
  font-size:.72rem;font-weight:600;
  color:#64748b;
  margin-bottom:.7rem;
  text-transform:uppercase;letter-spacing:.12em;
}
.todo-track ul{list-style:none;padding:0;display:flex;flex-wrap:wrap;gap:.45rem}
.todo-track li{
  display:inline-flex;align-items:center;gap:.4rem;
  font-size:.82rem;font-weight:500;
  padding:.3rem .75rem;
  border-radius:999px;
  background:#fff;
  color:#475569;
  border:1px solid #e2e8f0;
}
.todo-track li::before{content:"○";color:#94a3b8;font-size:.9rem}
.todo-track .done{background:#f0fdf4;color:#166534;border-color:#bbf7d0}
.todo-track .done::before{content:"●";color:#10b981}

/* ===== 列表（除 ref-list 外） ===== */
.report-body ul:not(.ref-list){padding-left:1.4rem;margin:0 0 1.1rem}
.report-body ul:not(.ref-list) li{margin:.35rem 0;color:#40414a}
.report-body ul:not(.ref-list) li::marker{color:#6366f1;font-weight:700}

/* ===== 表格 ===== */
.report-body table{
  width:100%;border-collapse:collapse;
  margin:1.5rem 0;font-size:.9rem;
  border:1px solid #e5e5e5;
  border-radius:10px;overflow:hidden;
}
.report-body th,.report-body td{
  padding:.75rem 1rem;text-align:left;
  border-bottom:1px solid #f3f3f2;
}
.report-body th{
  background:#fafafa;
  font-weight:600;color:#0a0a14;
  font-size:.78rem;
  text-transform:uppercase;letter-spacing:.06em;
}
.report-body tr:last-child td{border-bottom:none}
.report-body tr:hover td{background:#fafafa}

/* ===== 行内代码 & 代码块 ===== */
.report-body code{
  font-family:"JetBrains Mono","SF Mono",Menlo,Consolas,monospace;
  font-size:.85em;
  background:#f5f5f4;
  border:1px solid #ebebeb;
  padding:.1rem .4rem;
  border-radius:5px;
  color:#1a1a1a;
}
.report-body pre{
  background:#0a0a14;color:#e5e5e5;
  padding:1.25rem 1.5rem;
  border-radius:10px;
  overflow-x:auto;
  font-size:.85rem;line-height:1.6;
  margin:1.25rem 0;
}
.report-body pre code{background:none;border:none;padding:0;color:inherit}

/* ===== 引用块 ===== */
.report-body blockquote{
  border-left:3px solid #6366f1;
  padding:.75rem 0 .75rem 1.25rem;
  margin:1.5rem 0;
  color:#737380;font-style:italic;
}

/* ===== 页脚 ===== */
.report-footer{
  padding:1.5rem 3.5rem;
  border-top:1px solid #ececec;
  font-size:.78rem;color:#94a3b8;
  text-align:center;background:#fafafa;
  display:flex;align-items:center;justify-content:center;gap:.75rem;flex-wrap:wrap;
}
.report-footer::before,.report-footer::after{
  content:"";flex:0 0 24px;height:1px;background:#e5e5e5;
}

/* ===== 响应式 ===== */
@media (max-width:720px){
  body{padding:.5rem}
  .report-header{padding:2.25rem 1.5rem 1.75rem}
  .report-header h1{font-size:1.7rem}
  .report-body{padding:1.75rem 1.5rem 2.25rem}
  .report-footer{padding:1.25rem 1.5rem}
  .report-body h2{font-size:1.4rem}
}

/* ===== 暗色模式 ===== */
@media (prefers-color-scheme: dark){
  body{background:#0a0a0f;color:#f5f5f5}
  .report{
    background:#14141c;border-color:#26262e;
    box-shadow:0 1px 2px rgba(0,0,0,.4),0 24px 48px -12px rgba(0,0,0,.5);
  }
  .report-body p,.report-body ul:not(.ref-list) li{color:#d4d4d8}
  .report-body h2,.report-body h3,.report-body strong,.report-body th{color:#f5f5f5}
  .report-body code{background:#1d1d24;border-color:#26262e;color:#f5f5f5}
  .report-body th,.report-footer,.report-body tr:hover td{background:#14141c}
  .ref-list li{background:#14141c;border-color:#26262e}
  .ref-list li:hover{border-color:#6366f1}
  .exec-summary{background:linear-gradient(135deg,#1e1b3a 0%,#2a1a3a 100%);border-color:#3d2a5f}
  .exec-summary p{color:#d4d4d8}
  .todo-track{background:linear-gradient(135deg,#14141c 0%,#1a1a26 100%);border-color:#26262e}
  .todo-track li{background:#1d1d24;color:#a3a3a3;border-color:#26262e}
  .callout-infer{background:#2a1f0a;border-color:#5a3d10;color:#fde68a}
  .callout-uncertainty{background:#2a1010;border-color:#5a1f1f;color:#fecaca}
  .callout-info{background:#0a2a1a;border-color:#1f5a3d;color:#a7f3d0}
  .callout strong{color:inherit}
}

/* ===== 打印样式（导出 PDF 友好） ===== */
@media print{
  body{background:#fff;padding:0}
  .report{box-shadow:none;border:none;max-width:100%;border-radius:0}
  .report-header{
    background:#15152a !important;
    -webkit-print-color-adjust:exact;print-color-adjust:exact;
  }
  .report-body{padding:1.5rem 2rem}
  .ref-list li,.callout,.report-body h2{break-inside:avoid}
  .ref-list li:hover{transform:none;box-shadow:none;border-color:#ececec}
}
</style>
</head>
<body>
<div class="report">
  <div class="report-header">
    <div class="eyebrow">DriFox 深度研究报告</div>
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