---
name: visualization
description: DriFox 消息可视化输出规范。讲解/教学、数据对比与趋势曲线、架构/流程/时序、方案选型、UI 设计建议、指标状态展示等场景应默认加载本技能并产出可视化（echarts/mermaid/SVG/HTML widget），而非纯文字。轻入口包含可视化优先决策规则、通道选择、默认模板库与硬约束；各通道技术细节（CJK 字宽公式、Host API、CSS 变量、色板）按需读 references/。
---

# DriFox 可视化输出规范

> 移植自 workbuddy Visualizer Core Design System，按 DriFox 渲染架构重写。
> 架构事实：消息卡片是 **innerHTML 注入 + Chromium 83**，不是完整 web 沙箱。

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

### UI 设计专项

用户问「这个界面怎么设计」「样式怎么改」「给个布局方案」时，**效果图即回答**：内联 HTML widget 直接产出可见效果稿，文字只解释设计决策。多方案并排渲染，用 `context-tag[data-type="ask"]` 让用户点选继续。

## 通道选择（选定后按渐进加载表读对应 reference）

| 场景 | 通道 | 细节 |
|------|------|------|
| 自定义示意图/结构图/流程图 | 内联 `<svg>` | → `references/svg-guide.md` |
| 数据趋势、曲线、分布、占比 | ` ```echarts ` 代码块 | → `references/echarts-mermaid.md` |
| 顺序流程/时序/状态机/ER/甘特 | ` ```mermaid ` 代码块 | → `references/echarts-mermaid.md` |
| UI 效果稿、指标卡、对比卡、交互组件 | 内联 HTML | → `references/html-widget.md` |

## 渐进加载

| 场景 | 何时读 |
|------|--------|
| `references/svg-guide.md` | 画任何 SVG 前**必读**：CJK 字宽公式（不读中文必溢出）、间距公式、字体 style、arrow marker |
| `references/echarts-mermaid.md` | 用 echarts/mermaid 前必读：纯 JSON 约束（禁 formatter/回调/注释）、容器 400px 写死、流式行为 |
| `references/html-widget.md` | HTML 带交互或用主题色前必读：Host API 交互桥、CSS 变量、9-ramp 色板、Chromium 83 限制 |

## 硬约束（任何通道，常驻）

1. **`<script>` 永不执行**（innerHTML 注入后是死代码）。交互走 Host API。
2. **Chromium 83**：`context-stroke`、`:has()`、`subgrid`、`aspect-ratio` 不可用；flex/grid/CSS 变量/圆角可用。
3. 禁 `position: fixed`；外层容器透明，背景由宿主提供。
4. SVG 里不引用外部资源。
5. 宿主已深浅主题自适配（echarts init、CSS 变量注入），不写死背景色。

## 示意图三分法（决定画什么）

| 类型 | 适用 | 画法要点 |
|------|------|---------|
| 流程图 | 顺序过程、因果链、决策 | 单向流为主，≤5 节点；环形循环用编号步进器+文字回边，不画 ring |
| 结构图 | 包含关系、层级组成 | 外层容器 rx=20-24 最浅填充，内层 rx=8-12 次浅；内边距 ≥20px，≤3 层 |
| 示意图 | 建立直觉 | 物理对象画简化剖面（热水器=水箱+底部烧器）；抽象对象造空间隐喻（transformer=水平层叠板，哈希=漏斗撒进桶）；颜色编码强度而非类别 |

## 默认模板库（直接套用，变量已适配 DriFox）

**指标卡**（指标展示/状态总览首选项）：

```html
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px">
  <div style="background:var(--panel-soft);border-radius:8px;padding:1rem">
    <div style="font:400 13px sans-serif;color:var(--text-muted)">CPU 占用</div>
    <div style="font:500 24px sans-serif;color:var(--text);margin-top:4px">37%</div>
  </div>
</div>
```

**方案对比卡**（选型/方案对比首选项，特色方案加 2px 边框 + 徽章）：

```html
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px">
  <div style="background:var(--panel);border:2px solid var(--accent);border-radius:12px;padding:1rem 1.25rem">
    <span style="background:var(--accent-soft);color:var(--accent-text);font-size:12px;padding:2px 8px;border-radius:4px">推荐</span>
    <p style="font:500 15px sans-serif;color:var(--text);margin:8px 0 4px">方案 A</p>
    <p style="font:400 13px sans-serif;color:var(--text-secondary);margin:0">要点一句话</p>
  </div>
</div>
```

**数据记录卡**（实体/配置/联系人展示）：44px 头像圆（`var(--accent-soft)` 底 + `var(--accent-text)` 字）+ 15px/500 姓名 + 13px 副题，外层 `var(--panel)` 卡、0.5px `var(--border)`、圆角 12px、padding 1rem 1.25rem。

**交互解释器**（参数调节/行为演示）：HTML 控件（slider/toggle）+ 实时数值显示；追问入口用 `context-tag[data-type="ask"]`。逻辑用纯 CSS（checkbox hack）或静态展示，无 script。

通用规则：卡片 `background:var(--panel)`、`border:0.5px solid var(--border)`、圆角 12px；界面间距用 rem（1/1.5/2），组件内部用 px（8/12/16）；表格数据仍用 markdown 表写在正文，不塞进 widget。

## 复杂度预算（硬限制）

- 框副题 ≤ 12 个中文字
- 单图 ≤ 2 个色系 ramp
- 横向 tier ≤ 4 框（间距见 svg-guide）
- 流程图 ≤ 5 节点
- 结构嵌套 ≤ 3 层，容器内边距 ≥ 20px

## 多图叙事

- 复杂主题拆多张小图，不塞一张密图；每张图前一两句话说明它展示什么、与上一张的关系。
- 可视化块放回答**后半段**（流式渲染：闭合块才被扫描），文字解读写在图前。
- 图与图之间必须有文字衔接，不连续堆叠。

## 硬停止（不产可视化的条件）

- 单行事实问答、词典式查询
- 纯代码修改的 diff 说明
- 用户明确只要文字
- 闲聊与情感对话
- 纯 Qt 灰度渲染通道（`MarkdownBlockViewer`）：echarts/mermaid/CSS 变量全不可用，只输出 markdown + 代码块

**不暴露机制**：不写「根据可视化规范」「让我生成一个 echarts」「以下是 SVG 代码」。自然引入：「先看这张图」「流程是这样的」「效果如下」。

## 输出前自检

- [ ] echarts option 是纯 JSON：无函数、无注释、无尾逗号
- [ ] SVG viewBox 680，H 按最底元素算出；中文框宽用 CJK 公式算过
- [ ] 全文无 `<script>`、无 `onclick=`、无 `sendPrompt`；追问用 `context-tag[data-type="ask"]`
- [ ] 界面色用 CSS 变量，无硬编码 hex 界面色
- [ ] 可视化块在回答后半段，图前有引入文字
- [ ] 复杂度未超预算（节点数/嵌套层数/色系数）

## 示例

- 「讲解 FOPDT 模型」→ SVG 结构图（输入→一阶环节→滞后→输出）+ echarts 阶跃响应曲线，文字在图间串讲
- 「信号槽和回调有什么区别」→ 一张 SVG 双列对比图
- 「这个开关组件太丑了」→ 两个 HTML 效果稿并排（现状 vs 建议）+ 设计决策说明 + 点选追问
- 「最近 7 天内存占用变化」→ echarts 折线图，不用文字罗列数字
- 「微服务和单体怎么选」→ HTML 对比卡并排 + 一张选型决策 flowchart
