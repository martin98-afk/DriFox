# SVG 通道技术规范

画内联 SVG 前必读。viewBox 680 是一切坐标计算的基础，不遵守必翻车。

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
