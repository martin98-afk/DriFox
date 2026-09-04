# echarts / mermaid 通道技术规范

## echarts 通道

- **option 必须纯 JSON**：宿主把代码块内容 Base64 编码进 `data-echarts-json` 容器属性后 `JSON.parse` 解析，禁函数（`formatter`、回调）、禁注释、禁尾逗号。自定义文案用静态字符串字段。
- 容器固定 **400px 高、100% 宽**（宿主写死，option 无法改）：横向条形 ≤7 条舒适，更多条改纵向柱状或 markdown 表。深层级树图/图优先横向布局。
- 宿主免费提供：hover 工具栏、放大查看、PNG 导出、resize 自适应、深浅主题（`echarts.init(el, isDark ? 'dark' : undefined)`）。option 里不写死背景色、不写工具栏配置。
- 文字标签用 rich text 控制字号字重，只用 400/500 两档。

| 选型象限定位 | quadrantChart（英文）/ echarts 散点（中文） | 见 echarts-mermaid.md |

### 图型路由总表（按数据形态选图型，不只折线柱状饼图）

| 数据形态 | 图型 | 要点 |
|---------|------|------|
| 趋势/曲线 | line | 多系列 ≤3，面积图限单系列 |
| 对比 | bar | 横向 ≤7 条（400px 高限制） |
| 构成/占比 | pie / 堆叠 bar | 份额 ≤6，小份额合并「其他」 |
| 多维评分对比 | radar | 3-8 维，系列 ≤3，各维满刻度统一 |
| 层级占比 | treemap / sunburst | 层级 ≤3，treemap 优先横向 |
| 流量/资金/依赖分配 | sankey | 左进右出，层级 ≤4 |
| 依赖/关系网络 | graph（力导向） | 节点 ≤50，`layout:'force'` 纯 JSON，categories 分组着色 |
| 选型象限定位（中文标签） | scatter + markLine | 0-1 坐标，x/y=0.5 十字分割，markArea 四象限着色，点 label 显名称 |
| 单指标阈值状态 | gauge | ≤2 个并排（容器 400px 高限制），阈值分段色 |
| 二维密度 | heatmap | 类目规模 ≤20×7，visualMap 标明范围 |
| 金融 OHLC | candlestick | 涨红跌绿（见细则） |
| 量价/计数+比率组合 | 双轴 bar+line | 见细则 |
| 分布统计 | boxplot | 正文标注中位数与 IQR 含义 |

### echarts 设计细则

- 图例放顶部，`grid.top` 预留 32px；系列 ≤2 时省图例。
- 负数格式：`-¥5M` 而非 `¥-5M`；散点/气泡坐标轴范围留数据 ~10% 余量防裁剪。
- 颜色不足再区分：每个系列除颜色外加线型/填充差异，不只靠颜色。
- **markLine 基准线**：目标线/均值线/阈值线用 `markLine`（纯 JSON 支持），基准含义写进图例或副题。
- **双轴 bar+line**：`yAxis` 数组两项 + 各系列 `yAxisIndex`；左轴计数右轴比率，轴 `name` 写明单位。
- **K 线（candlestick）**：中文惯例涨红跌绿——`itemStyle.color` 涨（9-ramp c-red 600 `#A32D2D`）、`color0` 跌（c-green 600 `#3B6D11`）、`borderColor` 同步。单位放轴 `name`（如「价格 (¥)」）或副题，禁 formatter 拼 ¥ 前缀。日 K ≤90 根（400px 宽内可读上限）。

## mermaid 通道

- 首次遇到 ` ```mermaid ` 块时宿主动态加载 vendor（3.3MB，含 Chromium 83 polyfill），LLM 无感。
- 图类型路由：

| 需求 | mermaid 类型 |
|------|-------------|
| 顺序流程/因果链 | `flowchart TD/LR` |
| 消息时序 | `sequenceDiagram` |
| 数据库 schema | `erDiagram` |
| 状态机 | `stateDiagram-v2` |
| 甘特/计划 | `gantt` |
| 选型象限定位（**仅英文标签**） | `quadrantChart` |
| 分支策略/发布流 | `gitGraph` |
| 里程碑叙事 | `timeline` |
| 知识结构发散 | `mindmap` |

**quadrantChart 中文硬限制（runtime 实测）**：`x-axis`/`y-axis` 文本与点名在词法层拒绝中文（title 可中文），含中文必报 Lexical error。中文标签场景直接改走 echarts：scatter + 0-1 坐标 + `markLine` x/y=0.5 十字分割 + `markArea` 四象限着色（≤2 色系）+ 点 `label` 显名称，纯 JSON 且无词法限制。

gitGraph/timeline/mindmap 中文兼容已实测通过；渲染失败时降级——gitGraph → 文字步骤列表，timeline → HTML 垂直时间线（templates.md），mindmap → `flowchart` 层级布局。

- 数据库 schema / ERD 一律走 mermaid `erDiagram`，不手画 SVG。
- 环形循环不画成 ring：线性步骤 + 文字标注回边，或编号步骤列表。

## 流式行为（DriFox 特有，两通道都适用）

1. **差量渲染**：闭合的代码块才被宿主扫描渲染；未闭合块显示字面文本。可视化块放回答**后半段**，避免流式中途半成品。
2. **innerHTML 全量替换**：每次流式更新整个正文重建。SVG 内 CSS 动画会重启；`@keyframes` 只用于 ≤2s 循环，且包裹 `@media (prefers-reduced-motion: no-preference)`。
3. 完整写完一个 SVG/代码块再输出下一个，不边写边改。
