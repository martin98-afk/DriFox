# echarts / mermaid 通道技术规范

## echarts 通道

- **option 必须纯 JSON**：宿主把代码块内容 Base64 编码进 `data-echarts-json` 容器属性后 `JSON.parse` 解析，禁函数（`formatter`、回调）、禁注释、禁尾逗号。自定义文案用静态字符串字段。
- 容器固定 **400px 高、100% 宽**（宿主写死，option 无法改）：横向条形 ≤7 条舒适，更多条改纵向柱状或 markdown 表。深层级树图/图优先横向布局。
- 宿主免费提供：hover 工具栏、放大查看、PNG 导出、resize 自适应、深浅主题（`echarts.init(el, isDark ? 'dark' : undefined)`）。option 里不写死背景色、不写工具栏配置。
- 文字标签用 rich text 控制字号字重，只用 400/500 两档。

### echarts 设计细则

- 图例放顶部，`grid.top` 预留 32px；系列 ≤2 时省图例。
- 负数格式：`-¥5M` 而非 `¥-5M`；散点/气泡坐标轴范围留数据 ~10% 余量防裁剪。
- 颜色不足再区分：每个系列除颜色外加线型/填充差异，不只靠颜色。

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

- 数据库 schema / ERD 一律走 mermaid `erDiagram`，不手画 SVG。
- 环形循环不画成 ring：线性步骤 + 文字标注回边，或编号步骤列表。

## 流式行为（DriFox 特有，两通道都适用）

1. **差量渲染**：闭合的代码块才被宿主扫描渲染；未闭合块显示字面文本。可视化块放回答**后半段**，避免流式中途半成品。
2. **innerHTML 全量替换**：每次流式更新整个正文重建。SVG 内 CSS 动画会重启；`@keyframes` 只用于 ≤2s 循环，且包裹 `@media (prefers-reduced-motion: no-preference)`。
3. 完整写完一个 SVG/代码块再输出下一个，不边写边改。
