---
name: visualization
description: DriFox 消息可视化输出规范。当需要为用户生成图表、示意图、数据可视化、DAG 图、流程图、时序图、架构图，或准备使用 echarts/mermaid/SVG 代码块时必须加载本技能。包含 DriFox 渲染架构硬约束（innerHTML 注入、Chromium 83、无脚本执行）、Host API（context-tag 提问桥、copy 桥）、CJK 字宽公式、宿主 CSS 变量与 echarts/mermaid 通道细则。
---

# DriFox 可视化输出规范

> 移植自 workbuddy Visualizer Core Design System，按 DriFox 渲染架构重写。
> 与原版的根本差异：DriFox 消息卡片是 **innerHTML 注入 + Chromium 83**，不是完整 web 沙箱。

## 何时使用

- 用户要求「画个图 / 可视化 / 示意图 / 流程图 / 架构图 / 关系图」
- 内容适合用图表表达：流程、结构、数据对比、时序、状态机
- 准备输出 ` ```echarts `、` ```mermaid ` 或内联 `<svg>`/HTML widget

## 通道选择

| 需求 | 通道 | 渲染方 |
|------|------|--------|
| 交互式数据图（柱/线/饼/树图/DAG） | ` ```echarts ` 代码块 | 宿主 JS 扫描 `data-echarts-json` 容器并 init |
| 标准图（流程图/时序/ER/甘特/状态机） | ` ```mermaid ` 代码块 | 宿主懒加载 mermaid 10.9.1 渲染 |
| 自定义示意图/结构图 | 内联 `<svg>` 写在正文 | 浏览器直渲（raw HTML 透传，`safe=False`） |
| 带样式的信息组件 | 内联 HTML 写在正文 | 同上 |

数据库 schema / ERD 一律走 mermaid `erDiagram`，不手画 SVG。

## 硬约束

1. **`<script>` 永不执行**。消息 DOM 由 `innerHTML` 注入，script 标签是死代码。任何逻辑必须由宿主委托事件承担（见 Host API）。
2. **Chromium 83 内核**：
   - `stroke="context-stroke"` 不可用（Chromium 119+ 才支持）→ marker 落具体色或 `style="stroke: var(--text-secondary)"`
   - CSS `:has()`、`subgrid`、`aspect-ratio` 不可用
   - `flex`/`grid`/`CSS 变量`/`border-radius` 均可用
3. 禁 `position: fixed`（卡片高度自适应机制依赖正常文档流）。
4. 外层容器透明，背景由宿主提供。
5. 无 sanitizer：不要在 SVG 里引用外部资源；`<style>` 标签经 innerHTML 注入**有效**（与 script 不同），可用于定义图内 class。

## Host API（交互桥）

JS→Python 唯一通道是 `console.log('pywebview_action:...')` + `javaScriptConsoleMessage` 拦截。可用的是 document 级**事件委托**（innerHTML 替换后依然有效）：

| 写法 | 效果 |
|------|------|
| `<span class="context-tag" data-type="ask" data-action="ask" data-content="问题文本">问题文本</span>` | 点击即以该文本发送提问（`send_preset_question`） |
| `<button data-action="copy" data-copy="Base64文本">复制</button>` | 复制 Base64 解码后的文本 |
| `<a href="https://...">链接</a>` | 系统浏览器打开 |
| `<a href="file:///...">打开</a>` | 走 `acceptNavigationRequest` 系统打开 |

- `data-copy` 必须 Base64（JS 侧 `atob` 解码）。
- 没有 `sendPrompt` 函数调用，交互等价物就是 `context-tag[data-type="ask"]`。
- 纯 CSS 交互可用：`<input type="checkbox"> + <label>` 可做无 JS 的步进器/折叠。

## CSS 变量（明暗自适应）

宿主在 `:root` 注入全量主题变量，**用变量的组件深浅模式零成本适配**：

| 类别 | 变量 |
|------|------|
| 表面 | `--bg`(透明) `--panel` `--panel-soft` |
| 文本 | `--text` `--text-secondary` `--text-muted` |
| 边框 | `--border` `--border-strong` |
| 强调 | `--accent` `--accent-warm` `--accent-text` `--accent-soft` `--accent-soft-strong` `--accent-border-weak` `--accent-glow` |
| 语义 | `--success` `--danger` |
| 行底 | `--row-alt` `--row-hover` |

规则：
- HTML widget 的界面色一律用上述变量。
- 9-ramp 调色板降级为 **SVG 数据编码专用**（区分数据系列），不承担界面色。速查：亮底 50 填充 + 600 描边 + 800 标题；暗底 800 填充 + 200 描边 + 100 标题。
- echarts 深浅模式由宿主处理（`echarts.init(el, isDark ? 'dark' : undefined)`），option 里不要写死背景色。

9-ramp 色值（数据编码专用）：

| Class | 50 | 100 | 200 | 400 | 600 | 800 | 900 |
|-------|----|-----|-----|-----|-----|-----|-----|
| c-purple | #EEEDFE | #CECBF6 | #AFA9EC | #7F77DD | #534AB7 | #3C3489 | #26215C |
| c-teal | #E1F5EE | #9FE1CB | #5DCAA5 | #1D9E75 | #0F6E56 | #085041 | #04342C |
| c-coral | #FAECE7 | #F5C4B3 | #F0997B | #D85A30 | #993C1D | #712B13 | #4A1B0C |
| c-pink | #FBEAF0 | #F4C0D1 | #ED93B1 | #D4537E | #993556 | #72243E | #4B1528 |
| c-gray | #F1EFE8 | #D3D1C7 | #B4B2A9 | #888780 | #5F5E5A | #444441 | #2C2C2A |
| c-blue | #E6F1FB | #B5D4F4 | #85B7EB | #378ADD | #185FA5 | #0C447C | #042C53 |
| c-green | #EAF3DE | #C0DD97 | #97C459 | #639922 | #3B6D11 | #27500A | #173404 |
| c-amber | #FAEEDA | #FAC775 | #EF9F27 | #BA7517 | #854F0B | #633806 | #412402 |
| c-red | #FCEBEB | #F7C1C1 | #F09595 | #E24B4A | #A32D2D | #791F1F | #501313 |

## CJK 字宽公式（核心差异）

`rect_width = max(title_chars × 7, subtitle_chars × 6) + 24` 只适用拉丁字符，中文会溢出。逐字符累计：

```
char_w(c, fontSize) = fontSize × 1.00    （CJK：汉字、全角标点）
                    = fontSize × 0.55    （拉丁字母、数字、半角标点）
text_w(s, fontSize) = Σ char_w
rect_width  = max(text_w(title, 14), text_w(subtitle, 12)) + 24
rect_height = 单行 44 / 双行 56（不变）
```

示例：
- 标题「用户提问」= 4 × 14 = 56px → 框宽 ≥ 80
- 副题「解读意图与主题」= 7 × 12 = 84px → 框宽 ≥ 108
- 混排「TCP 握手序列」= 3×14 + 4×7.7 ≈ 73px → 框宽 ≥ 97

## SVG 规范

- `viewBox="0 0 680 H"`，680 不变，根元素 `width="100%"`。
- `H = 最底部元素 y + 高度 + 20`，不猜。
- 安全区 x∈[40,640]，y∈[40,H-40]，背景透明。
- 连接线 `<path>/<polyline>` 必须 `fill="none"`；框线 0.5px；文字 `dominant-baseline="central"`；禁旋转文字。
- 单图一 SVG。

### 间距公式（修正版：「4 框全宽 + 60px」在 680 内不成立）

| 横向框数 | 框宽 | 间距 | 总宽 |
|---------|------|------|------|
| 3 | ≤140 | 60 | ≤540 |
| 4 | 116 | 45 | 599 |

### 图内字体（宿主无预置 class，需自带 style）

SVG 前放一段标准 style：

```html
<style>
.t{font:400 13px sans-serif;fill:var(--text-secondary)}
.ts{font:400 12px sans-serif;fill:var(--text-muted)}
.th{font:500 14px sans-serif;fill:var(--text)}
</style>
```

字重只用 400/500 两档。字号下限 11px。sentence case。

### Arrow marker（修正版）

```html
<defs>
  <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5"
    markerWidth="6" markerHeight="6" orient="auto-start-reverse">
    <path d="M2 1L8 5L2 9" fill="none" style="stroke:var(--text-secondary)"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
  </marker>
</defs>
```

`var()` 使箭头随主题变色；`context-stroke` 在 Chromium 83 静默失效。

## echarts 通道细则

- **option 必须纯 JSON**：`JSON.parse` 解析，禁函数（`formatter`、回调）、禁注释、禁尾逗号。自定义文案用静态字符串字段。
- 容器固定 **400px 高、100% 宽**，深层级树图/图优先横向布局。
- 宿主免费提供：hover 工具栏、放大查看、PNG 导出、resize 自适应、深浅主题。不写进 option。
- 文字标签用 rich text 控制字号字重，遵守 400/500 两档。

## mermaid 通道细则

- 首次遇到 ` ```mermaid ` 块时宿主动态加载 vendor（3.3MB，含 Chromium 83 polyfill），LLM 无感。
- 图类型路由：

| 需求 | mermaid 类型 |
|------|-------------|
| 顺序流程/因果链 | `flowchart TD/LR` |
| 消息时序 | `sequenceDiagram` |
| 数据库 schema | `erDiagram` |
| 状态机 | `stateDiagram-v2` |
| 甘特/计划 | `gantt` |

- 环形循环不画成 ring：线性步骤 + 文字标注回边，或编号步骤列表。

## 流式行为（DriFox 特有）

1. **差量渲染**：闭合的代码块才被宿主扫描渲染；未闭合块显示字面文本。可视化块放回答**末尾**，避免流式中途半成品。
2. **innerHTML 全量替换**：每次流式更新整个正文重建。SVG 内 CSS 动画会重启；`@keyframes` 只用于 ≤2s 循环，且包裹 `@media (prefers-reduced-motion: no-preference)`。
3. 完整写完一个 SVG 再输出下一个，不边写边改。

## 复杂度预算（硬限制）

- 框副题 ≤ 12 个中文字
- 单图 ≤ 2 个色系 ramp
- 横向 tier ≤ 4 框（按间距公式表）
- 流程图 ≤ 5 节点
- 结构嵌套 ≤ 3 层，容器内边距 ≥ 20px

## 无障碍

- HTML widget 开头放视觉隐藏摘要（宿主无 `.sr-only`，内联实现）：

```html
<h2 style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)">一句话摘要</h2>
```

- SVG 用 `role="img"`，首子元素 `<title>` + `<desc>`。

## 纯 Qt 通道限制（灰度渲染器）

`MarkdownBlockViewer`（QTextEdit 富文本）通道下：echarts/mermaid 不渲染、CSS 变量/flex/grid 不可用、仅 HTML4 子集。该通道只输出纯 markdown + 代码块，不产可视化。
