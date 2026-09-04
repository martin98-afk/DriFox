# HTML widget 通道技术规范（交互/主题/色板）

带样式的信息组件、UI 效果稿、交互解释器画之前必读。外层容器透明，背景由宿主提供；禁 `position: fixed`。

## UI 设计专项（效果图即回答）

用户问「这个界面怎么设计」「样式怎么改」「给个布局方案」时，**效果图即回答**：内联 HTML widget 直接产出可见效果稿，文字只解释设计决策。多方案并排渲染（现状 vs 建议两稿并排），用 `context-tag[data-type="ask"]` 让用户点选继续。组件模板见 templates.md。

## Host API（交互桥）

JS→Python 唯一通道是 `console.log('pywebview_action:...')` + `javaScriptConsoleMessage` 拦截。可用的是 document 级**事件委托**（innerHTML 替换后依然有效）：

| 写法 | 效果 |
|------|------|
| `<span class="context-tag" data-type="ask" data-action="ask" data-content="问题文本">问题文本</span>` | 点击即以该文本发送提问（`send_preset_question`） |
| `<button data-action="copy" data-copy="Base64文本">复制</button>` | 复制 Base64 解码后的文本 |
| `<a href="https://...">链接</a>` | 系统浏览器打开 |
| `<a href="file:///...">打开</a>` | 走 `acceptNavigationRequest` 系统打开 |

- `data-copy` 必须 Base64（JS 侧 `atob` 解码）。
- 没有 `sendPrompt` 函数调用；SVG 里的 `onclick="sendPrompt(...)"` 在 DriFox 不可用，追问入口一律 `context-tag[data-type="ask"]`。
- 纯 CSS 交互可用：`<input type="checkbox"> + <label>` 可做无 JS 的步进器/折叠。
- `<script>` 永不执行（innerHTML 注入后是死代码），任何逻辑走上述通道。

## 宿主 CSS 变量（明暗自适应）

用变量的组件深浅模式零成本适配：

| 类别 | 变量 |
|------|------|
| 表面 | `--bg`(透明) `--panel` `--panel-soft` |
| 文本 | `--text` `--text-secondary` `--text-muted` |
| 边框 | `--border` `--border-strong` |
| 强调 | `--accent` `--accent-warm` `--accent-text` `--accent-soft` `--accent-soft-strong` `--accent-border-weak` `--accent-glow` |
| 语义 | `--success` `--danger` |
| 行底 | `--row-alt` `--row-hover` |

规则：
- HTML widget 界面色一律用上述变量，禁硬编码 hex 界面色。
- 9-ramp 调色板是 **SVG 数据编码专用**（区分数据系列），不承担界面色。

### Chromium 83 内核限制

- 不可用：`stroke="context-stroke"`、CSS `:has()`、`subgrid`、`aspect-ratio`。
- 可用：`flex`、`grid`、CSS 变量、`border-radius`、`<style>` 标签。

## 9-ramp 色板（数据编码专用）

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

亮底：50 填充 + 600 描边 + 800 标题；暗底：800 填充 + 200 描边 + 100 标题。单图 ≤2 个色系。

## 无障碍

- HTML widget 开头放视觉隐藏摘要（宿主无 `.sr-only`，内联实现）：

```html
<h2 style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)">一句话摘要</h2>
```

- SVG 用 `role="img"`，首子元素 `<title>` + `<desc>`。

## 纯 Qt 通道降级（灰度渲染器）

`MarkdownBlockViewer`（QTextEdit 富文本）通道下：echarts/mermaid 不渲染、CSS 变量/flex/grid 不可用、仅 HTML4 子集。该通道只输出纯 markdown + 代码块，不产可视化。
