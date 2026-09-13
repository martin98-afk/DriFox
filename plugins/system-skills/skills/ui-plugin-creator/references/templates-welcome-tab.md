# UI 插件代码模板 — 欢迎卡片插件 tab（HTML/echarts）

> 何时读：在欢迎卡片新增 tab、注入 HTML 或 echarts 图表（近 N 天趋势等）时。
> 前置依赖：patterns.md；echarts 走 ` ```echarts ` 代码块，勿用 QPainter 自绘。
> 产出：register_welcome_tab + render_func(ctx) -> HTML/echarts 代码块。

## 八、欢迎卡片插件 tab（HTML/echarts 注入会话初始卡片）

> 在欢迎卡片（会话初始卡片）新增一个 tab，内容为 **markdown 片段**
> （纯 HTML + 内联 CSS，或含 ` ```echarts ` 代码块的交互式图表），
> 经欢迎卡片 markdown→CodeWebViewer(QWebEngineView) 管线渲染。
> 参考实现：
> - `calendar` 插件（.drifox/plugins/calendar/ui/__init__.py）— HTML + onclick 交互
> - `context-stats` 插件（D:/work/drifox-plugins2/plugins/context-stats）— echarts 复杂图表

### 8.1 适用场景

| 用户说 | 用这个 |
|--------|--------|
| "欢迎卡片加个日历/天气/待办 tab" | ✅ welcome tab |
| "欢迎卡片加个 echarts 图表/统计趋势 tab" | ✅ welcome tab + echarts（§8.5） |
| "聊天里显示自定义 HTML 块" | 内容块渲染器（§二） |
| "弹出独立面板" | 浮动卡片（§一） |

**优势**：markdown/HTML + 内联 CSS，无需 QWidget；图表用 echarts 代码块（走主程序
骨架 JS 渲染），交互无需 Qt 信号链。

### 8.2 注册 API

```python
registry.register_welcome_tab(
    plugin_name="<plugin-name>",
    mode_key="<unique-mode>",   # tab 唯一标识，同时用作 welcome mode 值
    label="📅 日历",             # SegmentedWidget 上显示的文本
    render_func=lambda ctx: _render_html(),  # (context: dict) -> str(HTML 片段)
    priority=0,                 # 同 mode_key 时高者覆盖低者
)
```

**约束**：
- `mode_key` 避开系统内置 mode：`sessions` / `projects` / `changelog`
- `render_func` 返回 **markdown 片段**（含内联 `<style>`，或 ` ```echarts ` 代码块），会拼进欢迎卡片 body 走 markdown 管线
- 调用发生在主线程（同步渲染），**不要**在 render_func 里做网络/大文件读取
- 数据查询要做模块级缓存（db mtime + 日期作 key），避免切换 tab 重复查询

### 8.3 完整模板（calendar 参考实现）

```python
# -*- coding: utf-8 -*-
"""<plugin-name> UI 组件入口 — 欢迎卡片 <名称> tab

通过 register_welcome_tab 注册为欢迎卡片的新 tab（mode_key="<mode>"）。
render_func 返回独立 HTML 片段（内联样式 + 预渲染内容 + onclick 切换），
经欢迎卡片 markdown→CodeWebViewer(QWebEngineView) 管线渲染。

渲染约束（骨架 updateContent 用 innerHTML 注入内容）：
- `<script>` 标签不会执行（HTML 规范，innerHTML 注入的 script 被忽略）→
  内容由 Python 预渲染，交互用 onclick 内联立即执行函数
- `<style>` 标签注入后生效 → 样式全部内联在此
"""

import sys
from datetime import datetime

# 交互 JS（onclick 内联，无 <script> 依赖）。
# 占位符由 Python 注入：TODAY_Y / TODAY_M / TODAY_D（今天）、DELTA（±1）。
# 用 DOM API 构建节点（textContent），避免 HTML 字符串引号与 onclick 属性冲突。
_SHIFT_JS = """(function(b,dl){{
var w=b.parentNode.parentNode,y=+w.getAttribute('data-y'),m=+w.getAttribute('data-m');
m+=dl;if(m<1){{m=12;y--}}if(m>12){{m=1;y++}}
w.setAttribute('data-y',y);w.setAttribute('data-m',m);
// ... 用 createElement / textContent 重建内容 ...
}})(this,DELTA)"""


def _render_html(ctx: dict = None) -> str:
    """渲染 HTML 片段：内容由 Python 预渲染，切换走 onclick 内联 JS

    明暗适配：优先用主程序注入的 ctx["is_dark"]（跟随 Qt 主题），
    ctx 缺失时回退 prefers-color-scheme（跟随 OS）。
    """
    now = datetime.now()
    shift = _SHIFT_JS.replace("TODAY_Y", str(now.year)).replace("TODAY_M", str(now.month)).replace("TODAY_D", str(now.day))
    is_dark = ctx.get("is_dark") if isinstance(ctx, dict) else None
    light = "--text: #333; --muted: #999;"
    dark = "--text: #e6e6e6; --muted: #8a8a8a;"
    if is_dark is not None:
        root_css = f":root {{ {dark if is_dark else light} }}"
    else:
        root_css = (
            f":root {{ {light} }}"
            f"@media (prefers-color-scheme: dark) {{ :root {{ {dark} }} }}"
        )
    return f"""<div class="wrap" data-y="{now.year}" data-m="{now.month}">
  <button class="nav" onclick="{shift.replace('DELTA', '-1')}" title="上一项">‹</button>
  <div class="title">{now.year} 年 {now.month} 月</div>
  <button class="nav" onclick="{shift.replace('DELTA', '1')}" title="下一项">›</button>
</div>
<style>
.wrap {{ max-width: 560px; margin: 0 auto; font-family: inherit; }}
{root_css}
</style>
"""


def register_ui(registry):
    """注册 <plugin-name> 的 UI 组件（欢迎卡片 <名称> tab）"""
    # 清理旧子模块缓存（热重载兼容）
    prefix = "ui_plugin_<plugin_name>."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    registry.register_welcome_tab(
        plugin_name="<plugin-name>",
        mode_key="<mode>",
        label="<label>",
        render_func=lambda ctx: _render_html(ctx),
    )
```

### 8.4 关键约束与技巧（踩坑记录）

#### 8.4.1 `<script>` 不执行 → 内容 Python 预渲染 + onclick 内联 JS

骨架 `updateContent` 用 `innerHTML` 注入，**注入的 `<script>` 标签被浏览器忽略**
（HTML 规范行为），所以：

- ✅ **内容由 Python 预渲染**：日期网格/列表等在 render_func 里拼好 HTML
- ✅ **交互用 `onclick` 内联立即执行函数**：`onclick="(function(b,dl){...})(this,1)"`，
  `this` 是按钮元素，向上 `parentNode` 找容器
- ❌ 不要写 `<script>` 块、不要写 `<script src=...>`

#### 8.4.2 onclick 属性引号冲突 → 用 DOM API + 占位符替换

- JS 串里 **双花括号 `{{ }}` 转义**（Python f-string 里是字面量 `{`）
- **用占位符注入动态值**：`TODAY_Y` / `DELTA` 等由 `.replace()` 替换，
  避免直接把数字拼进 JS 导致引号/转义灾难
- JS 内部生成 DOM 用 `document.createElement` + `textContent`（不拼 HTML 字符串），
  天然规避 onclick 属性里嵌套引号的问题

#### 8.4.3 `<style>` 注入后生效 → 样式全内联

`<style>` 标签经 innerHTML 注入后**会生效**，所以样式全部写在 HTML 片段
尾部的 `<style>` 块里，无需外部 CSS 文件（骨架里也没有）。

#### 8.4.4 明暗主题：用主程序注入的 `ctx["is_dark"]`，prefers-color-scheme 只做兜底

**根因**：`prefers-color-scheme` 跟随 **OS 主题**，不跟随 Qt 应用主题
（Qt 用 theme_manager 控制，OS 亮色 + Qt 暗色时不生效）。

主程序 `_render_welcome_body` 已把 Qt 主题注入 ctx：`render_func({"is_dark": bool})`。
插件**必须**读 ctx 渲染，**不要**单独依赖 prefers-color-scheme：

```python
def _render_html(ctx: dict = None) -> str:
    light = "--text: #333;"
    dark = "--text: #e6e6e6;"
    is_dark = ctx.get("is_dark") if isinstance(ctx, dict) else None
    if is_dark is not None:
        root_css = f":root {{ {dark if is_dark else light} }}"
    else:
        # 兜底：ctx 缺失（旧主程序）时按 OS 自适应
        root_css = (f":root {{ {light} }}"
                    f"@media (prefers-color-scheme: dark) {{ :root {{ {dark} }} }}")
    return f"...<style>{root_css}</style>"
```

⚠️ 要点：
- `render_func` 签名必须接收 `ctx`（`lambda ctx: ...`），否则主程序注入的主题被丢弃
- 判断用 `ctx.get("is_dark")` 原值，**不要** `bool()` 包裹——`bool(None)` 是 False，
  会把"未注入"误判成浅色，正确写法是 `is_dark is not None` 区分"未注入"与"浅色"
- 部分文字颜色可能被骨架 CSS 覆盖（如链接色），必要时加 `!important`

#### 8.4.5 欢迎卡片 mode 持久化（无需插件处理）

- 插件 tab 的 mode 存独立配置字段 `welcome_plugin_tab`
- 重启后仅当该 tab **仍注册**时恢复；插件卸载/停用自动回退内置 mode
- 插件侧**不需要**做任何持久化代码

### 8.5 echarts 复杂图表渲染（参考实现：context-stats）

> 欢迎卡片 body 走主程序 markdown 管线，` ```echarts ` 代码块会被渲染成
> **交互式 echarts 图表**（无需 QWidget、无需内联 JS）。
> 参考实现：`context-stats` 插件（D:/work/drifox-plugins2/plugins/context-stats）
> — 单实例合并图表（左 token 面积图 + 右消息柱状图，双 Y 轴）。

#### 8.5.1 渲染链路（主程序内部，插件无需关心）

```
render_func 返回 markdown（含 ```echarts {JSON} 代码块）
→ _wrap_code_blocks_with_copy_button_web（lang=="echarts"）
→ <div class="echarts-container" data-echarts-json="{base64}">（固定 height: 400px）
→ 骨架 JS if(window.echarts) → echarts.init(el, 'dark') → chart.setOption(option)
→ ResizeObserver 自适应卡片宽度
```

**版本依赖**：需 DriFox ≥ 0.4.15（`_SKELETON_CACHE_VERSION >= 9`）——
light 骨架（欢迎卡片）才加载 echarts vendor（`window.echarts` 存在）。
旧版本欢迎卡片无 echarts，图表会退化为普通代码块，插件可加版本提示。

#### 8.5.2 render_func 返回 echarts 代码块

```python
def render_welcome_tab(ctx: Optional[dict] = None) -> str:
    """返回 markdown 片段：概要行 + echarts 代码块"""
    p = _palette(ctx.get("is_dark") if isinstance(ctx, dict) else True)
    return (
        "**近 14 天** · 估算 Token **8M** · 消息 **1.2k** 条\n\n"
        "```echarts\n"
        + json.dumps(_combined_option(daily_tokens, daily_messages, p),
                     ensure_ascii=False)
        + "\n```\n"
    )
```

#### 8.5.3 echarts JSON 的关键约束（坑）

- **JSON 不能携带 JS 函数**（base64 → JSON.parse 还原，函数会被丢掉）→
  `formatter` 必须用**字符串模板**：`"{value}M"`、`"{b}<br/>{a0}: {c0} tokens"`
- 数据预缩放 + 字符串模板补单位：`8000000 → 8M`（Python 侧按最大值选
  单位因子 1e6/1e3，formatter 拼后缀）— 见 context-stats `_scale_unit`
- **明暗适配**：色板在 Python 侧按 `ctx["is_dark"]` 生成（`_palette(is_dark)`），
  不要用 echarts 的 `theme` 参数（主程序骨架固定 `echarts.init(el, 'dark')`）
- 图表高度固定 400px（主程序内联 style），宽度自适应；多图表要纵向堆叠
  （每个 ` ```echarts ` 代码块独立成行，主程序逐个渲染）
- 无数据时**不要**输出 echarts 代码块，返回纯 markdown 提示行即可

#### 8.5.4 复杂图表能力（context-stats 已实现，可复用模式）

| 能力 | 实现方式 | 参考 |
|------|---------|------|
| 双 Y 轴合并图 | `yAxis: [{position:"left"},{position:"right"}]` + series 各自 `yAxisIndex` | context-stats `_combined_option` |
| 面积图渐变 | `areaStyle.color.type="linear"` + `colorStops` | 同上 |
| tooltip 双序列 | `formatter: "{b}<br/>{a0}: {c0}M tokens<br/>{a1}: {c1}k 条"` | 同上 |
| 右轴隐藏网格线 | 右轴 `splitLine: {"show": false}`（防与左轴重叠） | 同上 |
| 明暗色板 | `_palette(is_dark)` 返回完整 option 色板 | context-stats `_palette` |

### 8.6 验证清单

```
启动程序 → 欢迎卡片出现新 tab
1. tab 标签文字显示正确？           → label 参数
2. 内容渲染正确（无 script 报错）？  → 控制台无 JS 错误
3. 点击导航（‹/›）能切换？          → onclick 内联 JS 生效
4. 明暗主题切换颜色跟随？           → ctx["is_dark"] 注入生效（重点：Qt 暗色 + OS 亮色也生效）
5. echarts 图表渲染出来？           → window.echarts 存在（DriFox ≥ 0.4.15）
6. 图表明暗适配？                   → ctx["is_dark"] 切换色板（重启 Qt 主题验证）
7. 图表宽度自适应？                 → 卡片缩放时 chart.resize()（ResizeObserver）
8. 重启后 tab 记忆恢复？            → welcome_plugin_tab 字段
9. 插件热重载无异常？               → sys.modules 前缀清理
```

> 完整验证清单见 `checklist.md §12`。

---

