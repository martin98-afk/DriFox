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
mutex_groups:
  mode: ["--quick", "--thorough", "--deep"]
prompt_sections:
  --quick: "quick"
  --thorough: "multi"
  --deep: "multi"
  --html: "html"
---

## ⚙️ 行为规范（LLM 提示词正文）

### 1. 参数解析

`$ARGUMENTS` 是用户输入的完整字符串（不含 `/webresearch` 前缀）。

| 模式 | 行为 | 抓取预算 | 适用场景 |
|------|------|----------|----------|
| `--quick` | 1 跳：单次 `search_web` + 至多 1 次 `fetch_web` 深入 | ≤1 页 | 事实查询、定义、API 用法 |
| `--thorough` | 3 跳：`todo_write` 3-5 任务规划 → 1 次 `search_web` 选种 → 2 跳链接发现 + 抓取 | ≤6 页 | 多源验证、对比分析 |
| `--deep` | 5 跳：同上 + 4 跳链接发现 + 抓取 + 主题漂移监测 | ≤15 页 | 学术综述、技术深挖、争议性主题 |
| 无标志 | 默认 `--quick` | ≤1 页 | 兼容性好 |

- **`<query>`** 是 `$ARGUMENTS` 中去掉所有 `--flag` 后的剩余文本
- **`--save-to=PATH`** 自定义输出路径（默认 docs/research/research_<topic>_<ts>.md，--html 时扩展名为 .html）
- **`--html`** 开启 HTML 报告模式，与 `--deep` 或 `--quick` 均可搭配
- **未提供 query** → 报告"请提供研究主题"并停止

### 3. 工具后端降级策略

```
1. 检查 mcp__tavily__search 是否可用 → 用 Tavily 搜索（更精准）
2. 否则使用 search_web（DriFox 原生，支持 SerpAPI/DuckDuckGo 双回退）
3. 永远不要直接报"工具不可用"——总有降级路径
```

### 输出规范

每个模式产出结构化回答（带 URL 引用）。如指定 `--save-to`，写入对应路径。

**多平台抓取**时，用 `---` 分隔多个结果。

<!-- section:quick -->
### 4. Quick 模式流程

```
1. search_web(query=query, num_results=5)
2. 评估结果：
   - top1 已能直接回答 → 综合输出，不抓取
   - 需要细节 → fetch_web(url=top1_url, format=markdown)
3. 输出结构化回答（带 URL 引用）
   - --html 时用 HTML 模板渲染
   - 否则 Markdown 格式
4. 未指定 --save-to 则提供写入建议
```
<!-- end -->

<!-- section:multi -->
### 5. Deep / Thorough 模式流程（多跳链接遍历）

**共性规则**：
```
- 同跳内多个 fetch_web 可并行
- 跳间必须串行（跳 N 依赖跳 N-1 的抓取内容）
- 每跳上限 3 页（thorough 3 跳、deep 5 跳）
- 总预算：thorough ≤6 页、deep ≤15 页
- 预算耗尽 / 候选池为空 / 漂移超 60% → 停止进入综合
```

**阶段 A：规划**
```
1. 解析参数，识别模式
2. todo_write([3-5 子主题规划，含探索图状态：聚焦/已抓/待抓/偏离度])
3. search_web(query, num_results=10)
4. 从 top 10 选 3 个种子进入跳 1
```

**阶段 B：跳 1（种子抓取）**
```
5. 并行 fetch_web(url1), fetch_web(url2), fetch_web(url3)
6. 正则提取所有 [text](url) 链接 → 候选池
7. 输出进度："✓ 跳 1/N · 已抓 3 页 · 发现 X 个候选"
8. todo_write 更新探索图
```

**阶段 C：跳 2-N（链接发现 + 抓取）**
```
9.  LLM 硬规则过滤候选池
10. LLM 自主决策选 top 3
11. todo_write 更新探索图
12. 并行 fetch_web 选中的 3 个
13. 提取新链接 → 候选池
14. LLM 自评主题偏离度
15. 输出进度
16. 偏离度 > 60% → 跳到 D；30-60% → 标注警告继续；<30% → 继续
17. 跳完最后一跳 / 预算耗尽 / 漂移停止 → D
```

**阶段 D：综合 + 写报告**
```
18. 综合所有页面写报告（执行摘要/主体发现/引用源/不确定性）
19. 追加探索路径章节
20. --save-to 写入指定路径
21. --html 时用 HTML 模板渲染
```

#### 5.1 链接选择硬规则（顺序应用）

```
硬规则 1 — 去重：剔除已抓过的 URL
硬规则 2 — 域名限频：同一 apex domain 最多保留 2 个
硬规则 3 — 模式过滤：剔除 *.pdf/mailto:/javascript:/login/signup 等
硬规则 4 — 路径深度：URL 路径段 ≤ 5
硬规则 5 — 关键词匹配：候选池 > 15 时保留相关链接
```

#### 5.2 LLM 自主决策引导

```
"从候选链接中选 3 个最值得抓取的。
 优先级：与 query 相关性 > 来源权威性 > 内容新鲜度 > 页面深度浅"
```

#### 5.3 主题漂移监测（LLM 自评，每跳必做）

```
自评 prompt：
"当前主题: <query>
 本轮抓取内容摘要: <LLM 根据本轮 3 页面内容概括>
 本轮内容与主题的相关性（0-100%）: <数字>
 漂移原因（如相关度 < 70%）: <简要说明>
 候选池剩余链接与主题的相关度高于本轮吗？: <是/否/不确定>"

偏离度 = 100% - 相关性
- > 60% → 立即停止，进入阶段 D
- 30-60% → 标注 "⚠ 主题漂移警告"，继续
- < 30% → 正常继续
```
<!-- end -->

<!-- section:html -->
### 2. 统一 HTML 报告风格定义

当 `--html` 参数启用时，报告输出必须使用以下 HTML 模板渲染。

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{TITLE} — DriFox 研究报告</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-size:16px}
body{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  color:#0a0a14;background:#f6f6f4;line-height:1.7;padding:3rem 1.25rem;
}
.report{max-width:920px;margin:0 auto;background:#fff;border-radius:20px;overflow:hidden;border:1px solid #ececec}
.report-header{
  background:radial-gradient(at 18% 8%,rgba(99,102,241,.32) 0px,transparent 50%),
            radial-gradient(at 82% 92%,rgba(236,72,153,.22) 0px,transparent 50%),
            linear-gradient(135deg,#0a0a14,#15152a,#1c1c38);
  color:#fff;padding:3.5rem 3rem 3rem;
}
.report-header h1{font-size:2.4rem;font-weight:600;line-height:1.22}
.report-body{padding:3rem 3.5rem 3.5rem}
.exec-summary{
  background:linear-gradient(135deg,#f5f3ff,#fdf4ff);border:1px solid #ede9fe;
  padding:1.5rem 1.75rem;border-radius:12px;margin-bottom:2.75rem;
}
.report-body h2{font-size:1.7rem;margin:2.75rem 0 1.1rem;padding-bottom:.75rem;border-bottom:2px solid #6366f1}
.report-body h3{font-size:1.15rem;font-weight:600;margin:2rem 0 .8rem}
.report-body p{color:#40414a;margin:0 0 1.1rem}
/* 行内引用：[N] 锚点链接（点击跳转到引用源列表） */
.cite-ref{font-size:.65rem;font-weight:700;vertical-align:super;line-height:0;margin:0 .1rem}
.cite-ref a{color:#fff;background:#6366f1;padding:.1rem .42rem;border-radius:4px;text-decoration:none;transition:background .15s}
.cite-ref a:hover{background:#4f46e5;text-decoration:none}
/* 兼容旧版 source-tag（保留向后兼容） */
.source-tag{font-size:.65rem;font-weight:700;color:#fff;background:#6366f1;padding:.1rem .42rem;border-radius:4px;vertical-align:super}
.callout{margin:1.5rem 0;padding:1.1rem 1.4rem;border-radius:10px;border:1px solid}
.callout-infer{background:#fffbeb;border-color:#fde68a;color:#78350f}
.callout-uncertainty{background:#fef2f2;border-color:#fecaca;color:#7f1d1d}
.callout-info{background:#ecfdf5;border-color:#a7f3d0;color:#064e3b}
.ref-list{list-style:none;padding:0;display:grid;gap:.6rem;counter-reset:ref}
.ref-list li{position:relative;padding:.9rem 1rem .9rem 3.4rem;border:1px solid #ececec;border-radius:10px;scroll-margin-top:1rem}
.ref-list .ref-index{position:absolute;left:.85rem;width:1.85rem;height:1.85rem;background:#6366f1;color:#fff;border-radius:7px;text-align:center;line-height:1.85rem;font-weight:700;font-size:.85rem}
.ref-list .ref-link{color:#6366f1;text-decoration:none;font-weight:500;word-break:break-all}
.ref-list .ref-link:hover{text-decoration:underline;color:#4f46e5}
.ref-list .ref-url{display:block;font-size:.72rem;color:#94a3b8;margin-top:.2rem;word-break:break-all;font-family:monospace}
.ref-list li:target{background:linear-gradient(135deg,#eef2ff,#fdf4ff);border-color:#6366f1;box-shadow:0 0 0 2px rgba(99,102,241,.15)}
/* 全局链接：所有 a 标签默认新窗口打开 */
.report-body a{color:#6366f1;text-decoration:none;word-break:break-word}
.report-body a:hover{text-decoration:underline;color:#4f46e5}
.todo-track{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:1.1rem 1.4rem;margin-bottom:2rem}
.exploration-path{margin-top:2.75rem;padding-top:2rem;border-top:1px solid #ececec}
.path-stats{display:flex;flex-wrap:wrap;gap:.5rem;margin:1rem 0 1.5rem}
.path-stats .stat{padding:.4rem .9rem;background:#ecfdf5;border:1px solid #a7f3d0;border-radius:999px;font-size:.82rem;color:#064e3b}
.hop-timeline{list-style:none;padding:0;padding-left:2.5rem;position:relative}
.hop-timeline::before{content:"";position:absolute;left:.9rem;top:.5rem;bottom:.5rem;width:2px;background:#10b981}
.hop-num{position:absolute;left:-2.5rem;width:1.85rem;height:1.85rem;background:#10b981;color:#fff;border-radius:50%;text-align:center;line-height:1.85rem}
.report-body table{width:100%;border-collapse:collapse;margin:1.5rem 0}
.report-body th,.report-body td{padding:.75rem 1rem;border-bottom:1px solid #f3f3f2}
.report-body code{font-family:monospace;font-size:.85em;background:#f5f5f4;border:1px solid #ebebeb;padding:.1rem .4rem;border-radius:5px}
.report-body pre{background:#0a0a14;color:#e5e5e5;padding:1.25rem 1.5rem;border-radius:10px;overflow-x:auto}
.report-footer{padding:1.5rem 3.5rem;border-top:1px solid #ececec;font-size:.78rem;color:#94a3b8;text-align:center;background:#fafafa}
@media (prefers-color-scheme:dark){
  body{background:#0a0a0f;color:#f5f5f5}
  .report{background:#14141c;border-color:#26262e}
  .report-body p,.report-body ul li{color:#d4d4d8}
  .report-body h2,.report-body h3,.report-body strong{color:#f5f5f5}
  .ref-list li{background:#14141c;border-color:#26262e}
  .ref-list .ref-link{color:#a5b4fc}
  .ref-list .ref-link:hover{color:#c7d2fe}
  .ref-list .ref-url{color:#64748b}
  .cite-ref a{background:#818cf8}
  .cite-ref a:hover{background:#a5b4fc;color:#1e1b3a}
  .ref-list li:target{background:linear-gradient(135deg,#1e1b3a,#2a1a3a);border-color:#6366f1}
  .exec-summary{background:linear-gradient(135deg,#1e1b3a,#2a1a3a);border-color:#3d2a5f}
}
@media (max-width:720px){
  .report-header{padding:2.25rem 1.5rem}
  .report-header h1{font-size:1.7rem}
  .report-body{padding:1.75rem 1.5rem}
}
@media print{
  body{background:#fff;padding:0}
  .report{box-shadow:none;border:none;max-width:100%;border-radius:0}
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
    </div>
  </div>
  <div class="report-body">{CONTENT}</div>
  <div class="report-footer">由 DriFox 深度研究报告引擎生成</div>
</div>
</body>
</html>
```

**占位符**：`{TITLE}`→报告标题, `{TIMESTAMP}`→ISO时间戳, `{MODE}`→模式标签, `{CONTENT}`→HTML正文

---

#### 2.1 行内引用协议（铁律 — 这是 LLM 最容易出错的地方）

**核心原则**：行内引用使用**编号 `[N]` + 锚点链接**，而不是把完整 URL 写进正文。

**为什么**：把 URL 写进正文行内既冗长又容易格式错误（漏写、拼写错、标点粘到 URL 里）。编号系统让 LLM 只在**一处**（引用源列表）写完整 URL，其他地方只写编号 `[N]`，从根本上消灭不稳定。

**规则**：

```
铁律 1（结构）：行内引用必须用这个完整 HTML 片段（不要省略任何部分）：
  <sup class="cite-ref"><a href="#src-N">[N]</a></sup>
  其中 N 是从 1 开始的整数，与下方引用源列表中的 id="src-N" 严格一一对应。

铁律 2（一一对应）：[N] 中的 N 必须出现在 `## 引用源` 列表里，且列表中必须有 id="src-N" 的条目。
  - 不允许出现孤儿 [N]（正文有但列表里没有）
  - 不允许出现孤儿 id（列表有但正文没引用）

铁律 3（紧贴事实）：[N] 必须紧跟在它所支撑的事实/数据/结论之后，标点之前。
  ✓ 正确：Python 3.12 于 2023 年 10 月发布 <sup class="cite-ref"><a href="#src-1">[1]</a></sup>。
  ✗ 错误：Python 3.12 于 2023 年 10 月发布。[1]
  ✗ 错误：Python 3.12 于 2023 年 10 月发布 <sup>[1]</sup>（缺 class 和 href）

铁律 4（一处多引）：同一句话支持多个独立来源时，用多个 [N][M] 紧贴排放：
  PEP 695 的设计目标包括类型参数语法简化与运行时开销消除 <sup class="cite-ref"><a href="#src-2">[2]</a></sup><sup class="cite-ref"><a href="#src-3">[3]</a></sup>。

铁律 5（不引常识）：纯常识、定义、不需要来源即可成立的内容，不要加 [N]（避免噪声）。
```

**完整示例**（一句话 + 多源）：

```html
<p>Python 3.12 引入 PEP 695 类型参数语法，将运行时类型检查开销降低约 40%<sup class="cite-ref"><a href="#src-1">[1]</a></sup><sup class="cite-ref"><a href="#src-2">[2]</a></sup>，并在 2023 年 10 月正式发布<sup class="cite-ref"><a href="#src-1">[1]</a></sup>。</p>
```

---

#### 2.2 引用源列表协议（与行内引用配套）

**触发**：`## 引用源`（在 markdown 源码中）→ 渲染为 `<ul class="ref-list">`。

**每条格式**（必须严格遵守）：

```html
<li id="src-N">
  <span class="ref-index">N</span>
  <a class="ref-link" href="https://example.com/path" target="_blank" rel="noopener noreferrer">来源标题</a>
  <span class="ref-url">https://example.com/path</span>
</li>
```

**要点**：
- `id="src-N"` 必须与行内 `[N]` 严格对应
- `target="_blank" rel="noopener noreferrer"` **不可省略**（让浏览器新窗口打开且不传 referrer）
- `class="ref-link"` 让标题文字也作为可点击链接
- `class="ref-url"` 在标题下方显示完整 URL（视觉冗余但便于核对）

**完整示例**：

```html
<ul class="ref-list">
  <li id="src-1">
    <span class="ref-index">1</span>
    <a class="ref-link" href="https://www.python.org/downloads/release/python-3120/" target="_blank" rel="noopener noreferrer">Python 3.12.0 Release</a>
    <span class="ref-url">https://www.python.org/downloads/release/python-3120/</span>
  </li>
  <li id="src-2">
    <span class="ref-index">2</span>
    <a class="ref-link" href="https://peps.python.org/pep-0695/" target="_blank" rel="noopener noreferrer">PEP 695 – Type Parameter Syntax</a>
    <span class="ref-url">https://peps.python.org/pep-0695/</span>
  </li>
  <li id="src-3">
    <span class="ref-index">3</span>
    <a class="ref-link" href="https://docs.python.org/3.12/whatsnew/3.12.html" target="_blank" rel="noopener noreferrer">What&rsquo;s New In Python 3.12</a>
    <span class="ref-url">https://docs.python.org/3.12/whatsnew/3.12.html</span>
  </li>
</ul>
```

---

#### 2.3 URL 清洗铁律（链接可点击的根本保证）

**生成 `<a href="...">` 时必须按以下顺序清洗 URL**：

```
步骤 1（去包装）：如果 URL 出现在括号 ()、方括号 []、尖括号 <>、引号 "" '' 内，剥掉这些外层包装字符
步骤 2（去尾标）：URL 末尾不允许出现以下字符之一 —— . , ; : ! ? ) ] } ' " < > 。
  - 若出现，必须从 URL 字符串中删除（不是只放在 <a> 外）
  - 反例：href="https://example.com."（结尾的 . 会让浏览器请求失败或跳转错误）
步骤 3（去首标）：URL 开头不允许出现以下字符之一 —— . , ; : ! ? ( [ { ' " < > 。
步骤 4（强制绝对）：URL 必须以 http:// 或 https:// 开头。
  - 若只有 www. 开头或裸域名，必须补 https://
步骤 5（HTML 转义）：URL 中的 & 必须写为 &amp;，引号必须写为 &quot;（在 href 属性里）
步骤 6（保留尾部斜杠）：路径至少含 /，避免 https://example.com 这种 host-only 形式（除非本来就是）
```

**反面教材**（都会让链接不可点击或跳转失败）：

```html
<!-- ❌ 1. 漏了 target 和 rel，点进去会跳走离开报告 -->
<a href="https://example.com">来源</a>

<!-- ❌ 2. URL 尾巴粘了个句号 -->
<a href="https://example.com." target="_blank" rel="noopener noreferrer">来源</a>

<!-- ❌ 3. URL 包含了外层括号 -->
<a href="（https://example.com）" target="_blank" rel="noopener noreferrer">来源</a>

<!-- ❌ 4. 用了相对路径或缺协议 -->
<a href="//example.com" target="_blank" rel="noopener noreferrer">来源</a>
<a href="example.com" target="_blank" rel="noopener noreferrer">来源</a>

<!-- ❌ 5. href 里的 & 没转义 -->
<a href="https://example.com/?a=1&b=2" target="_blank" rel="noopener noreferrer">来源</a>
<!-- 应为 href="https://example.com/?a=1&amp;b=2" -->
```

---

#### 2.4 兜底自动链接规则（防 LLM 漏写）

如果 LLM 在正文中出现了**裸 URL**（如 `https://example.com/article`）且没包 `<a>` 标签，必须主动转换为：

```html
<a href="https://example.com/article" target="_blank" rel="noopener noreferrer">https://example.com/article</a>
```

触发条件：行内文本匹配 `https?://[^\s<>\"\'()\[\]{}]+`，且不在 `href="..."` 中、也不在 `<code>` 块里。

---

#### 2.5 其他 HTML 转换规则

- `## 摘要/关键信息/主体发现` → `<h2>`；`###` → `<h3>`；列表保持 `<ul>/<li>`
- `## 执行摘要` → `<div class="exec-summary">`
- `## 不确定性` → `<div class="callout callout-uncertainty">`
- `⚠️ 推断` → `<div class="callout callout-infer">`
- Deep/Thorough 的探索路径 → `<section class="exploration-path">` 包裹

---

#### 2.6 输出前自检清单（必须 100% 通过，否则重写）

在 `</body>` 之前，LLM 必须在心里（不输出）逐条核对：

```
□ 检查 1：所有 `<a href="...">` 都有 `target="_blank" rel="noopener noreferrer"`
□ 检查 2：所有 `<a href="...">` 的 URL 都通过 2.3 的 6 步清洗
□ 检查 3：所有 `<sup class="cite-ref">` 的 [N] 都在 `## 引用源` 里有 id="src-N" 对应
□ 检查 4：`## 引用源` 里所有 id="src-N" 都被正文 `<sup class="cite-ref">` 引用过（无孤儿源）
□ 检查 5：正文无裸 URL（除非已经在 <a> 内或 <code> 内）
□ 检查 6：所有 `<ul class="ref-list">` 的 `<li>` 都有 `id="src-N"`
□ 检查 7：开头编号从 1 连续递增到 N，无跳号
```

**任何一项不通过 → 重写该部分，再走一遍自检。**

---

#### 2.7 Markdown 模式引用规则（不开 --html 时）

Markdown 模式下，渲染器**不会**自动给裸 URL 加链接（没有 autolink 扩展），必须用 `[text](url)` 形式：

```
- 每个有源支撑的事实写为：[事实描述](url)
- URL 仍需走 2.3 的 6 步清洗
- 不需要写 id / class，但 LLM 仍需在文末维护一个 markdown 引用源列表：

  ## 引用源
  1. [来源标题](url)
  2. [来源标题](url)
```

行内若要简短引用，可使用 `([来源标题](url))` 句式；不要直接写裸 URL。
<!-- end -->
