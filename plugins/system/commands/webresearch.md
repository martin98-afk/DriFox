---
description: 深度网络研究，支持快速查询与多跳调研，自动降级到 DriFox 原生 Web 工具
type: prompt
argument-hint:
  "[--quick]": "快速模式：1 次搜索 + 至多 1 次抓取（事实查询、定义）"
  "[--thorough]": "深度模式：3 跳多源调研 + TodoWrite 追踪（多源验证、对比分析）"
  "[--deep]": "极深模式：5 跳多源调研 + 主题漂移监测（学术综述、技术深挖）"
  "[--html]": "HTML 报告模式：输出为带统一样式的 HTML 报告（配合 --save-to 使用）"
  "[--save-to=]": "指定报告输出路径（默认 docs/research/research_<topic>_<ts>.md，--html 时扩展名为 .html）"
  "<query>": "研究主题（必填，搜索关键词或自然语言问题）"
---

## ⚙️ 行为规范（LLM 提示词正文）

### 1. 参数解析

`$ARGUMENTS` 是用户输入的完整字符串（不含 `/research` 前缀）。

| 模式 | 行为 | 抓取预算 | 适用场景 |
|------|------|----------|----------|
| `--quick` | 1 跳：单次 `search_web` + 至多 1 次 `fetch_web` 深入 | ≤1 页 | 事实查询、定义、API 用法 |
| `--thorough` | 3 跳：`todo_write` 3-5 任务规划 → 1 次 `search_web` 选种 → 2 跳链接发现 + 抓取 | ≤6 页 | 多源验证、对比分析 |
| `--deep` | 5 跳：同上 + 4 跳链接发现 + 抓取 + 主题漂移监测 | ≤15 页 | 学术综述、技术深挖、争议性主题 |
| 无标志 | 默认 `--quick` | ≤1 页 | 兼容性好 |

- **`<query>`** 是 `$ARGUMENTS` 中去掉所有 `--flag` 后的剩余文本
- **`--save-to=PATH`** 自定义输出路径（默认 docs/research/research_<topic>_<ts>.md，--html 时扩展名为 .html）
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

/* ===== 探索路径（Deep/Thorough 模式） ===== */
.exploration-path{
  margin-top:2.75rem;
  padding-top:2rem;
  border-top:1px solid #ececec;
}
.path-stats{
  display:flex;flex-wrap:wrap;gap:.5rem;
  margin:1rem 0 1.5rem;
}
.path-stats .stat{
  display:inline-flex;align-items:center;
  padding:.4rem .9rem;
  background:#ecfdf5;
  border:1px solid #a7f3d0;
  border-radius:999px;
  font-size:.82rem;
  color:#064e3b;
  font-weight:500;
}
.path-stats .stat strong{
  color:#047857;
  font-weight:700;
  font-variant-numeric:tabular-nums;
  margin:0 .15rem;
}
.hop-timeline{
  list-style:none;padding:0;
  position:relative;
  padding-left:2.5rem;
}
.hop-timeline::before{
  content:"";position:absolute;
  left:.9rem;top:.5rem;bottom:.5rem;
  width:2px;
  background:linear-gradient(180deg,#10b981 0%,#6366f1 100%);
}
.hop-timeline > li{
  position:relative;
  margin-bottom:1.5rem;
  padding-bottom:1.5rem;
  border-bottom:1px dashed #e5e5e5;
}
.hop-timeline > li:last-child{
  margin-bottom:0;padding-bottom:0;border-bottom:none;
}
.hop-num{
  position:absolute;left:-2.5rem;top:0;
  display:inline-flex;align-items:center;justify-content:center;
  width:1.85rem;height:1.85rem;
  background:linear-gradient(135deg,#10b981 0%,#059669 100%);
  color:#fff;
  border-radius:50%;
  font-size:.78rem;font-weight:700;
  box-shadow:0 2px 6px rgba(16,185,129,.3);
  z-index:1;
}
.hop-action{
  display:block;
  font-size:.85rem;font-weight:600;
  color:#0a0a14;
  margin-bottom:.55rem;
}
.hop-timeline ul{
  list-style:none;padding:0;margin:0;
  display:flex;flex-direction:column;gap:.4rem;
}
.hop-timeline ul li{
  font-size:.88rem;color:#40414a;
  padding:.5rem .75rem;
  background:#f8fafc;
  border-radius:6px;
  border-left:2px solid #10b981;
}
.hop-timeline ul li::before{content:none}

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
- Deep/Thorough 模式的「探索路径」 → `<section class="exploration-path">` 包裹：
  - 标题用 `<h2>🔍 探索路径</h2>`
  - 统计区用 `<div class="path-stats">` 含 4 个 `<span class="stat">`
  - 遍历轨迹用 `<ol class="hop-timeline">`，每跳是 `<li>`：
    - 跳数徽章 `<span class="hop-num">跳 N</span>`
    - 跳说明 `<span class="hop-action">...</span>`
    - 抓取页面列表 `<ul><li><a>...</a> → 关键信息: ...</li></ul>`
  - 触发漂移停止时，附加 `<blockquote>` 用 ⚠ 标记

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

### 5. Deep / Thorough 模式流程（多跳链接遍历）

**所有跳的共性规则**：

```
- 同跳内多个 fetch_web 可以并行调用（节省时间）
- 跳间必须串行（跳 N 的目标选择依赖跳 N-1 的抓取内容）
- 每跳上限 3 页（thorough 共 3 跳、deep 共 5 跳）
- 总抓取预算：thorough ≤6 页、deep ≤15 页
- 抓取预算耗尽、候选池为空、主题漂移超 60% — 任一触发即停止遍历进入综合
```

**阶段 A：规划**（仅 deep / thorough）

```
1. 解析 $ARGUMENTS，识别 --deep / --thorough / --html / --save-to / query
2. todo_write([3-5 个子主题规划]):
   例：
   - "子主题1: 核心概念定义"
   - "子主题2: 最新 2026 实践"
   - "子主题3: 对比方案"
   - "子主题4: 反例/陷阱"
   - "[探索图] 主题聚焦: <query>"
   - "[探索图] 已抓: []"
   - "[探索图] 待抓: []"
   - "[探索图] 主题偏离度: 0%"
   - "[跳1] 选 3 个种子（来自 search_web top 10）"
   - "[跳2] 评估候选池 + 选 3 个"
   - "[跳3] 评估候选池 + 选 3 个"
   - "[跳4] 评估候选池 + 选 3 个"（仅 deep）
   - "[跳5] 评估候选池 + 选 3 个"（仅 deep）
   - "[综合] 写报告 + 探索路径"
3. search_web(query, num_results=10) → 拿到 top 10 个种子 URL
4. LLM 从 top 10 选 3 个进入跳 1（按来源权威性 + 与 query 相关性）
```

**阶段 B：跳 1（种子抓取）**

```
5. 并行 fetch_web(url1), fetch_web(url2), fetch_web(url3) ← 跳 1 可以并行
6. 从 3 个页面 markdown 中正则提取所有 [text](url) 形式的链接 → 候选池
7. 输出实时进度（见第 6 节格式）:
   "✓ 跳 1/5 · 已抓 3 页 · 发现 47 个候选链接"
8. todo_write 更新探索图：已抓列表 + 待抓列表（从候选池筛 top 3）
```

**阶段 C：跳 2-N（链接发现 + 抓取，每跳流程相同）**

```
9.  LLM 应用硬规则过滤候选池（见第 5.1 节）
10. LLM 自主决策选 top 3（见第 5.2 节引导 prompt）
11. todo_write 更新探索图
12. 并行 fetch_web 选中的 3 个
13. 提取新链接 → 加入候选池
14. LLM 自评主题偏离度（见第 5.3 节）
15. 输出实时进度
16. 偏离度 > 60% → 跳到阶段 D
    偏离度 30-60% → todo 标注 "⚠ 主题漂移警告"，继续下一跳
    偏离度 < 30% → 继续下一跳
17. 跳完最后一跳 / 预算耗尽 / 漂移停止 → 阶段 D
```

**阶段 D：综合 + 写报告**

```
18. 综合所有抓取页面写主体报告（执行摘要 / 主体发现 / 引用源 / 不确定性）
19. 追加「探索路径」章节（见第 7、8 节）
20. --save-to 写入指定路径或默认 docs/research/research_<topic>_<ts>.{md,html}
21. --html 时使用统一 HTML 模板渲染
```

#### 5.1 链接选择硬规则（必须执行，顺序应用）

```
硬规则 1 — 去重:
  剔除所有已抓过的 URL（跨跳记忆，状态在 todo 中维护）

硬规则 2 — 域名限频:
  同一 apex domain 最多保留 2 个未抓 URL
  例：抓了 example.com/a 后，example.com/b 仍可抓，example.com/c 被剔除

硬规则 3 — 模式过滤:
  剔除以下模式:
  - *.pdf, *.zip, *.docx, *.doc, *.xlsx, *.pptx
  - mailto:*, javascript:*, tel:*
  - 纯 #anchor 链接（fragment-only）
  - 包含 "login", "signup", "logout", "cart", "checkout" 的 URL

硬规则 4 — 路径深度限制:
  URL 路径段（按 / 分割）≤ 5
  例：/a/b/c 允许，/a/b/c/d/e/f 拒绝
  理由：过深的 URL 往往是搜索页、分页、无限滚动

硬规则 5 — 关键词匹配（候选池超限时）:
  若过滤后候选池仍 > 15 个，保留:
  a) 最近一跳页面中提及的链接（出现位置越靠前越优先）
  b) 与原始 query 共享 ≥2 个关键词的链接
  仍超限则按 (a) 截断
```

#### 5.2 LLM 自主决策引导 prompt

在硬规则过滤后的池子里调用 LLM：

```
"从以下候选链接中选 3 个最值得下一跳抓取的。
 评判标准（按优先级）:
 1. 与原始主题『{query}』的相关性（最优先）
 2. 是否包含其他来源未覆盖的独特视角/数据/案例
 3. 来源权威性（学术/官方/一线工程师博客 > 聚合站/营销内容）
 4. 内容深度（深度文章 > 列表/科普）

 选完后对每个 URL 写一句『关键信息』，用于报告「探索路径」章节。
 关键信息格式: 这个页面会贡献什么当前未覆盖的内容（10-30 字）。"
```

#### 5.3 主题漂移监测

```
每跳完成后，LLM 自评:
  偏离度 = (本跳新学到的内容中与原始 query 弱相关的内容比例) × 100

阈值:
  < 30%：  正常推进
  30-60%： 在 todo 中标注「⚠ 主题漂移警告，下一跳收紧选择标准」并继续
  > 60%：  停止遍历，跳到阶段 D 写报告，报告「探索路径」末尾说明:
           "在跳 N 触发主题漂移上限（{实际偏离度}%）。
            已学到的核心信息: X, Y, Z。
            未深入的子主题: A, B（如需可重新查询）。"

漂移评估是 LLM 自评，不是硬计算，因为「相关性」本质是语义判断。
```

### 6. 实时进度输出格式（仅 deep / thorough）

quick 模式不输出（避免噪音）。deep / thorough 模式在每跳完成后立即输出当前进度（不进最终报告）：

```
[Deep 模式 · 5 跳研究] 主题: RAG vs Fine-tuning 选型
  ✓ 跳 1/5 · 已抓 3 页 · 发现 47 个候选链接
  ✓ 跳 2/5 · 已抓 6 页 · 发现 23 个候选链接 · 主题偏离度 12%
  ⚠ 跳 3/5 · 已抓 9 页 · 发现 12 个候选链接 · 主题偏离度 38% (警告：已收紧选择)
  ○ 跳 4/5 · 进行中...
  ✓ 跳 5/5 · 已抓 14 页 · 主题偏离度 18% · 触发综合阶段
```

**符号约定**：
- `✓` 完成且无警告
- `⚠` 完成但有漂移警告
- `○` 进行中
- `✗` 该跳全部抓取失败

**触发条件**：
- 每跳 fetch_web 全部返回后立即输出
- 漂移 >60% 触发停止时，最后一行用 `✗ 停止：主题漂移超限 (67%)`

### 7. 证据与不确定性

**每个事实陈述必须带源 URL**。**没有源就不写。**

模板：
```markdown
[事实陈述](https://source-url.com) — 来源：[站点名](https://source-url.com)
```

如果没有可引用的源，使用：
```markdown
⚠️ 推断：基于 [...上下文...]，可能结论为 [...](理由)
```

### 8. 输出格式

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

## 🔍 探索路径

**总抓取**: 14 页 | **完成跳数**: 5/5 | **主题偏离度**: 18% | **耗时**: 4m 12s

### 遍历轨迹

**跳 1** — 从 search_web 选 3 个种子
- [a.com/article-1](url) → 关键信息: RAG 的 3 个核心优势
- [b.org/paper-2](url) → 关键信息: Fine-tuning 的算力成本
- [c.io/blog-3](url) → 关键信息: 2026 年新趋势 hybrid 方案

**跳 2** — 基于跳 1 候选池选 3 个
- [d.com/post-4](url) → 关键信息: ...
- [e.org/article-5](url) → 关键信息: ...
- [f.io/blog-6](url) → 关键信息: ...

**跳 3** — ...
- ...

（如触发漂移停止，附加）:
> ⚠ 在跳 3 触发主题漂移上限（67%），已停止遍历。
> 已学到的核心信息: X, Y, Z。未深入的子主题: A, B（如需可重新查询）。

## 不确定性
- [已知不知道的]
```

`--html` 时输出 HTML 文件，内容遵循统一风格模板：
- 标题区使用 `<div class="report-header">`，含标题、时间戳、模式标签
- `## 执行摘要` → `<div class="exec-summary"><p>...</p></div>`
- `## 主体发现` → `<h2>主体发现</h2>`，子节使用 `<h3>`
- 每个 `[事实](url)` → `<a href="url">事实</a><sup class="source-tag">源</sup>`
- `## 引用源` → `<h2>引用源</h2>` + `<ul class="ref-list">`
- `## 🔍 探索路径` → `<section class="exploration-path">`（见第 2 节追加的转换规则）
- 跳数徽章、统计 pill、timeline 列表：按 CSS 类名对应渲染
- `## 不确定性` → `<div class="callout callout-uncertainty">`
- `⚠️ 推断` → `<div class="callout callout-infer">`
- Todo 追踪信息 → `<div class="todo-track">`（如有）

### 9. 边界

**会做**：
- 主动降级到现有可用工具
- 为每个事实附来源
- 尊重用户指定的输出路径
- 用 `todo_write` 跟踪 deep 模式任务
- 严格遵守每跳 ≤3 页、总抓取预算（thorough 6、deep 15）
- 任何 fetch_web 失败不静默吞掉，要在 todo 和报告中可见
- 主题漂移警告必须在 todo 和报告「探索路径」中都体现
- thorough / deep 模式必带「探索路径」章节
- 引用源数量在 thorough / deep 模式无上限（来自实际抓取的页面）
- quick 模式行为完全保持现状（不输出探索路径、不维护 todo 探索图）

**不会做**：
- 在没有源的情况下编造数据
- 执行搜索以外的副作用（除非用户明确 --save-to）
- 反复重试失败的搜索（最多 2 次补搜）
- 修改项目代码、运行构建、提交 Git
- 不在 deep 模式中并行 search_web 多个 query（保持 1 次 search + 多跳抓的范式）
- 不抓 PDF/视频/播客（只抓 HTML）
- 不主动翻墙/绕开反爬
- 不在没抓到任何内容时"编造"——直接报错退出
- 不修改 quick 模式的核心行为（1 跳逻辑保持现状）
- 不引入新的工具（仍只用 search_web / fetch_web / todo_write / write_file）