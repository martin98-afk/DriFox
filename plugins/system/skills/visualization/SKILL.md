---
name: visualization
description: 可视化优先输出技能。凡回答涉及以下场景必须先加载本技能，跳过本技能直接用纯文字回答是错误行为：讲解/教学/原理（为什么、怎么运作、是什么）、数据与对比（怎么选、区别、优缺点、趋势、曲线、占比、层级、分布）、架构/流程/时序/状态机、UI 设计与界面效果（长什么样、给个样式、怎么改好看）、指标与状态展示/监控总览、金融行情与 K 线、多维评分对比、流量/资金桑基流向、选型象限定位、里程碑时间线、数据周报、数学/物理公式与方程。产出通道：echarts 图表/mermaid 图/SVG 示意图/HTML widget（效果稿/指标卡/对比卡）/正文 LaTeX 公式（宿主 KaTeX 自动渲染）；复杂场景走场景配方。加载后先读通道选择表，各通道技术细节与模板按需读 references/。
---

# DriFox 可视化输出规范

> 架构事实：消息卡片是 **innerHTML 注入 + Chromium 83**，不是完整 web 沙箱。
> 需要执行脚本时唯一通道是 ` ```widget ` 围栏（沙箱 iframe + `__drifoxBridge` 白名单）；` ```html ` 与内联 SVG 里的 `<script>` 一律不执行。

## 第一动作：先想视觉，再想文字

一条信息画出来比写出来更清楚，就画。视觉负责结构与直觉，文字负责论证与细节。不需要等用户开口要图。

自问一个问题：**这段内容里有没有结构、对比、趋势、流程或状态？** 有 → 至少出一个可视化块；没有 → 纯文字。拿不准时，给一个小的可视化块，好过三段文字。

### 三类触发

| 类型 | 信号 | 动作 |
|------|------|------|
| 显式 | 用户说「画个图/可视化/示意图/曲线/界面长什么样」 | 必出可视化，无例外 |
| 主动 | 讲解概念、数据对比与趋势、架构/流程/时序、方案选型、指标状态、UI 设计建议 | 默认出可视化，文字围绕图展开 |
| 规格 | 用户给的是名词短语而非动词句：「X vs Y 对比」「订单状态机」「登录页布局」「内存占用曲线」 | 直接渲染该产物，不要用文字描述它 |

- **讲解/教学必出图**：「讲解 X」「教我 X」「X 是什么原理」必配概念图、流程图或响应曲线，唯一例外是词典式单词查询。
- **markdown 表格不是等价物**：用户点名「对比表/时间线/状态机/仪表盘」时，要的是渲染出的视觉组件，不是一张 markdown 表。

## 通道选择（选定后按渐进加载表读对应 reference）

| 场景 | 通道 | 细节 |
|------|------|------|
| 自定义示意图/结构图/流程图 | 内联 `<svg>` | → `references/svg-guide.md` |
| 数据趋势/曲线/分布/占比 | ` ```echarts ` 代码块 | → `references/echarts-mermaid.md` |
| 层级占比/多维评分/仪表盘/热力/桑基/K线/象限定位（中文标签） | ` ```echarts ` 代码块 | → `references/echarts-mermaid.md`（图型路由总表） |
| 流程/时序/状态机/ER/甘特/象限选型（英文）/分支流/里程碑 | ` ```mermaid ` 代码块 | → `references/echarts-mermaid.md`（图型路由总表） |
| UI 效果稿/指标卡/对比卡/交互组件 | 内联 HTML | → `references/html-widget.md` |
| 要执行脚本的交互组件（滑块/输入联动/实时计算/stepper/点节点改状态） | ` ```widget ` 代码块 | → `references/html-widget.md`（「交互通道」节） |
| 数学/物理公式、方程、推导 | 正文 LaTeX 定界符 | 宿主 KaTeX 自动渲染，规则见下节 |
| 监控总览/选型报告/性能排查/数据周报（复合场景） | 配方组合 | → `references/playbooks.md` |

### 数学公式（正文级渲染，非图表通道）

- 行内用 `$...$` 或 `\(...\)`；块级（独立居中）用 `$$...$$` 或 `\[...\]`。直接写在 markdown 正文，宿主 KaTeX 自动渲染。
- 宿主按 GitHub 规则识别：开 `$` 右侧不能紧邻数字或空白（`$100`、`$HOME` 不会误渲染）；内容含中文不渲染；未闭合不渲染。
- **公式 ≠ 图像**：需要表达函数形状、响应曲线、数据分布时仍走 echarts 画图，LaTeX 只负责表达式本身。两者常配对：公式给定义，echarts 给直觉。
- 公式本身不需要「文字解读」包裹，符号含义紧随公式后用一行文字交代。

## 渐进加载

| 场景 | 何时读 |
|------|--------|
| `references/svg-guide.md` | 画任何 SVG 前**必读**：示意图三分法、CJK 字宽公式（不读中文必溢出）、间距公式、字体 style、arrow marker；带动画必读「CSS 动画」节 |
| `references/echarts-mermaid.md` | 用 echarts/mermaid 前必读：图型路由总表、纯 JSON 约束（禁 formatter/回调/注释）、容器 400px 写死、流式行为 |
| `references/html-widget.md` | HTML 带交互、UI 效果稿或用主题色前必读：UI 设计专项、Host API 交互桥、CSS 变量、9-ramp 色板、Chromium 83 限制 |
| `references/templates.md` | 套指标卡/对比卡/数据记录卡/交互解释器/决策矩阵/时间线/进度清单/sparkline/状态徽章模板时 |
| `references/playbooks.md` | 命中监控总览/选型报告/性能排查/数据周报等复合场景，或多图叙事排版时 |

## 硬约束（任何通道，常驻）

1. **`` ```html `` 与内联 SVG 里的 `<script>` 永不执行**（innerHTML 注入后是死代码），交互走 Host API。要真执行脚本必须换 ` ```widget ` 围栏（沙箱 iframe，见「交互通道」节）。
2. **Chromium 83**：`context-stroke`、`:has()`、`subgrid`、`aspect-ratio`、`transform-box: fill-box` 不可用（fill-box 使动画 origin 静默失效）；flex/grid/CSS 变量/圆角可用。
3. 禁 `position: fixed`；外层容器透明，背景由宿主提供。
4. SVG 里不引用外部资源。
5. 宿主已深浅主题自适配（echarts init、CSS 变量注入），不写死背景色。

## 交互通道（要执行 JS 时用 ```widget）

` ```html ` 里的 `<script>` 会被剥离（永久死代码），**只有** ` ```widget ` 能执行脚本。判定：

| 场景 | 通道 |
|------|------|
| 只要展示（指标卡、效果稿、静态 SVG、对比卡） | ` ```html ` / ` ```svg ` |
| 要滑块、输入联动、实时计算、点节点改状态 | ` ```widget ` |
| 数据是表格/序列/层级 | ` ```echarts ` |

` ```widget ` 内容跑在沙箱 iframe 里（源为 opaque，拿不到宿主 DOM / 本地文件 / 网络）。可用能力只有：

- `window.__drifoxBridge.getTheme()` → Promise，返回 `{isDark, chartBg, textColor, vars}`
- `window.__drifoxBridge.sendPrompt(text)` → 把文本当追问发出
- `window.__drifoxBridge.storage.get(k)` / `.set(k, v)` → 会话级，get 返回 Promise
- 宿主主题变量（`--panel` `--text` `--accent` `--border` `--r-md` …）由宿主自动注入沙箱 `:root`，照常 `var(--panel)` 使用，深浅主题自动跟随
- **不用自己上报高度**：宿主已用 ResizeObserver + 轮询自动撑高 iframe

硬约束：

- `<script>` 必须**内联**（外链 script 被 CSP 拦）；`<style>` 同样内联。
- `fetch` / `XHR` / `WebSocket` 全禁（`connect-src 'none'`）。需要数据就把数据写死在内容里。
- 别在 ` ```html ` 里写脚本凑 —— 会被静默剥离，产物是个不动的壳。

## SVG 图节点可点

`<g>` / `<rect>` / `<circle>` 上挂 `.context-tag` 即走同一条点击链（不必是 `<span>`）：

```html
<g class="context-tag" data-type="ask" data-content="展开讲讲这一层">
  <rect x="100" y="20" width="180" height="44" rx="8" stroke-width="0.5" />
  <text x="190" y="42" text-anchor="middle" dominant-baseline="central">编码层</text>
</g>
```

- 只对**整组**挂，不要给组里每个图元各挂一次（会重复触发）。
- `data-content` 写完整追问句 —— 它是点击后真正发出的文本。
- 悬停反馈由宿主 `svg .context-tag:hover { opacity: .78 }` 提供，不要自己写 hover 样式。
- 别用 `onclick="sendPrompt(...)"` —— 那是别家 API，DriFox 里没有，且事件属性会被净化掉。

## 复杂度预算（硬限制）

- 框副题 ≤ 12 个中文字
- 单图 ≤ 2 个色系 ramp
- 横向 tier ≤ 4 框（间距见 svg-guide）
- 流程图 ≤ 5 节点
- 结构嵌套 ≤ 3 层，容器内边距 ≥ 20px

## 硬停止（不产可视化的条件）

- 单行事实问答、词典式查询
- 纯代码修改的 diff 说明
- 用户要的是可直接复制的代码/配置文本（交付代码块，不渲染组件）
- 用户明确只要文字
- 闲聊与情感对话
- 纯 Qt 灰度渲染通道（`MarkdownBlockViewer`）：echarts/mermaid/CSS 变量全不可用，只输出 markdown + 代码块

**不暴露机制**：不写「根据可视化规范」「让我生成一个 echarts」「以下是 SVG 代码」。自然引入：「先看这张图」「流程是这样的」「效果如下」。

## 输出前自检

- [ ] echarts option 是纯 JSON：无函数、无注释、无尾逗号
- [ ] SVG viewBox 680，H 按最底元素算出；中文框宽用 CJK 公式算过
- [ ] ` ```html ` / SVG 里无 `<script>`、无 `onclick=`、无 `sendPrompt`；追问用 `context-tag[data-type="ask"]`（SVG 图形节点也可挂）
- [ ] 用 ` ```widget ` 时：脚本内联（禁外链 script）、无 `fetch`/`XHR`、数据写死在内容里
- [ ] 界面色用 CSS 变量，无硬编码 hex 界面色
- [ ] 可视化块在回答后半段，图前有引入文字
- [ ] 复杂度未超预算（节点数/嵌套层数/色系数）
