# SVG 通道技术规范

画内联 SVG 前必读。viewBox 680 是一切坐标计算的基础，不遵守必翻车。

## 示意图三分法（决定画什么）

| 类型 | 适用 | 画法要点 |
|------|------|---------|
| 流程图 | 顺序过程、因果链、决策 | 单向流为主，≤5 节点；环形循环用编号步进器+文字回边，不画 ring |
| 结构图 | 包含关系、层级组成 | 外层容器 rx=20-24 最浅填充，内层 rx=8-12 次浅；内边距 ≥20px，≤3 层 |
| 示意图 | 建立直觉 | 物理对象画简化剖面（热水器=水箱+底部烧器）；抽象对象造空间隐喻（transformer=水平层叠板，哈希=漏斗撒进桶）；颜色编码强度而非类别 |

## 基本规范

- `viewBox="0 0 680 H"`，680 不变，根元素 `width="100%"`。
- `H = 最底部元素 y + 高度 + 20`，不猜。
- 安全区 x∈[40,640]，y∈[40,H-40]，背景透明。
- 连接线 `<path>/<polyline>` 必须 `fill="none"`；框线 0.5px；文字 `dominant-baseline="central"`；禁旋转文字。
- 单图一 SVG。
- `<script>` 死代码；`<style>` 标签经 innerHTML 注入有效，可定义图内 class。
- 不引用外部资源（无 sanitizer，但加载会失败）。

## CJK 字宽公式（核心差异，不遵守中文必溢出）

`rect_width = max(title_chars × 7, subtitle_chars × 6) + 24` 只适用拉丁字符。中文逐字符累计：

```
char_w(c, fontSize) = fontSize × 1.00    （CJK：汉字、全角标点）
                    = fontSize × 0.55    （拉丁字母、数字、半角标点）
text_w(s, fontSize) = Σ char_w
rect_width  = max(text_w(title, 14), text_w(subtitle, 12)) + 24
rect_height = 单行 44 / 双行 56
```

示例：
- 标题「用户提问」= 4 × 14 = 56px → 框宽 ≥ 80
- 副题「解读意图与主题」= 7 × 12 = 84px → 框宽 ≥ 108
- 混排「TCP 握手序列」= 3×14 + 4×7.7 ≈ 73px → 框宽 ≥ 97

## 间距公式（680 总宽约束下）

| 横向框数 | 框宽 | 间距 | 总宽 |
|---------|------|------|------|
| 3 | ≤140 | 60 | ≤540 |
| 4 | 116 | 45 | 599 |

## 图内字体（宿主无预置 class，需自带 style）

SVG 前放一段标准 style：

```html
<style>
.t{font:400 13px sans-serif;fill:var(--text-secondary)}
.ts{font:400 12px sans-serif;fill:var(--text-muted)}
.th{font:500 14px sans-serif;fill:var(--text)}
</style>
```

字重只用 400/500 两档。字号下限 11px。sentence case。

## Arrow marker（每个有箭头的 SVG 必须包含）

```html
<defs>
  <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5"
    markerWidth="6" markerHeight="6" orient="auto-start-reverse">
    <path d="M2 1L8 5L2 9" fill="none" style="stroke:var(--text-secondary)"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
  </marker>
</defs>
```

`var()` 使箭头随主题变色；`stroke="context-stroke"` 在 Chromium 83 静默失效，禁用。

## 结构嵌套（结构图）

- 外层容器：大圆角 rect，rx=20-24，最浅填充（ramp 50），0.5px 描边。
- 内层区域：中圆角 rect，rx=8-12，次浅填充（ramp 100-200）。
- 每层容器内边距 ≥20px，最多 2-3 层嵌套。

## 数据编码配色（9-ramp，数据专用，不做界面色）

亮底速查：50 填充 + 600 描边 + 800 标题；暗底速查：800 填充 + 200 描边 + 100 标题。完整色板见 `html-widget.md`。

## CSS 动画（动效场景必读，实战踩坑沉淀）

`<script>` 死代码，SMIL 无法响应 reduced-motion，动效一律 CSS：动画写在图内 `<style>`，全部 keyframes 与 animation 包进 `@media (prefers-reduced-motion: no-preference)`。

**origin 规则（最大坑）**：禁 `transform-box: fill-box`，Chromium 83 对 SVG 静默失效（元素不动或绕错心转）。origin 一律写 view-box 显式坐标，每个元素各自的 origin 用内联 style 覆盖：

```html
<path class="wing" style="transform-origin:264px 79px" d="..."/>
```

**感知阈值**：小于 15px 的元素做 scale/translate 动画肉眼不可见（6px 高的鸟 scaleY 0.8 全程只变 1.2px，用户会报「没动」）。小元素拆结构放大力臂：鸟 = 身体一点 + 左右翅两条 path，绕翅根 rotate ±30°。

**角色骨骼动画**：`d: path()` 关键帧可做关节联动（腿蹬踏板），要求所有帧命令结构完全一致（如 `M x y C x y x y x y l x y`），8 帧近似圆周已平滑。踝点坐标 = 驱动点（踏板）逐帧位置，膝盖用 C 控制点前偏表达。

**多部件同步**：联动件（轮/曲柄/踏板/腿/链条）共用同一 `animation-duration`；0% 关键帧几何对齐（曲柄臂端点 = 踏板静态位 = 踝始态）。相位差 180° 的配对件用**反相 keyframes**，禁用 animation-delay 表达相位（delay 是时间平移，位移场不同的部件会脱节——腿 delay 半周期而踏板位移场不同，脚会离开踏板）。链条等环带用 dashoffset 流动，速度 = 周长 ÷ 周期。

**无缝循环背景**：图案复制 + 视宽平移，循环点无跳变：

```html
<g class="roadtex"><g id="rtex">…线条…</g><use href="#rtex" x="680"/></g>
```
```css
.roadtex { animation: texmove 2.7s linear infinite; }
@keyframes texmove { to { transform: translateX(-680px); } }
```

**速度一致性**：地面物移动速度必须 ≈ 轮缘线速度（2πr ÷ 转周期），否则打滑感；视差分层（远景云 20s+，中景 2~3s）。dashoffset 正值向路径起点方向移动，流向必须与画面运动方向一致（车向右，路面纹理向左流）。

**层序**：远景 → 中景 → 影子 → 主体（远侧肢在车架下、近侧肢在躯干上）→ 前景动效（速度线）。

## 图节点可点

节点组挂 `.context-tag` 即可点击追问 —— 宿主的点击委托已扩展到 SVG 元素：

```html
<g class="context-tag" data-type="ask" data-content="展开讲讲这一层">
  <rect x="100" y="20" width="180" height="44" rx="8" stroke-width="0.5" />
  <text class="th" x="190" y="42" text-anchor="middle" dominant-baseline="central">编码层</text>
</g>
```

- 挂**整组**不挂单个图元；`data-content` 写完整追问句。
- 悬停反馈由宿主 `svg .context-tag:hover { opacity: .78 }` 提供，不要再自己写 hover 样式。
- 不要用 `onclick`、也不要用 `<a>` 包 SVG 节点：事件属性会被净化，且页面里没有 `sendPrompt` 全局函数。
