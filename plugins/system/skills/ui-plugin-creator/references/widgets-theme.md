# 可复用控件 — 主题色与上下文映射

> 来自 `context-usage-stats` 等项目提炼的主题色转换与注入模式。
> 索引和设计原则见 `widgets.md`。
> 上下文注入（拉模型）的核心概念见 `SKILL.md §4.1`。

---

## 一、问题背景

UI 插件运行在浮动卡片里，卡片背景可能很暗（半透明黑），而主程序的主题色是给"普通界面"用的。如果直接用 `isDarkTheme()` 或主程序的 `text_primary`，会出现：

1. 卡片背景偏暗时，黑色字看不见
2. 主题色变化时，图表不更新
3. 不同卡片大小下，标签溢出

**解决方案**：
- 父卡片从 `context_provider()` 拉取 ctx，转成图表可用的 QColor 字典
- 把字典注入到所有子 widget（`_StatCard` / `_BarChartWidget` / ...）
- 子 widget 缓存 `_colors`，在 `paintEvent` / `_apply_card_style` 中用

---

## 二、上下文 → QColor 字典

### 2.1 `_make_chart_colors_from_context(ctx: dict) -> dict`

```python
from PyQt5.QtGui import QColor


def _make_chart_colors_from_context(ctx: dict) -> dict:
    """将 context 中的 colors 映射为图表可用的 QColor 字典

    Args:
        ctx: UIPluginRegistry 注入的上下文，包含：
            - colors: dict, 含 accent / success / warning / border / card_bg 等键
            - is_dark: bool, 当前是否深色主题
            - font_family: str, 当前字体（如 "Microsoft YaHei"）
            - font_size: int, 当前基准字号（通常 14）

    Returns:
        与 _default_chart_colors() 输出一致，所有值都是 QColor 或 str
    """
    raw = ctx.get("colors", {})
    is_dark = ctx.get("is_dark", True)

    def _qcolor(key: str, fallback_light: str, fallback_dark: str) -> QColor:
        """从 ctx.colors 取色，无则按 is_dark 选 fallback"""
        val = raw.get(key, "")
        if val:
            return QColor(val)
        return QColor(fallback_dark if is_dark else fallback_light)

    accent = _qcolor("accent", "#2878dc", "#62a0ea")
    success = _qcolor("success", "#00a888", "#50e3c2")

    return {
        "bar_fill": accent.lighter(110),
        "bar_border": accent,
        "line": success,
        "line_fill": QColor(success.red(), success.green(), success.blue(), 60),
        "point": success,
        "grid": _qcolor("border", "#cccccc80", "#ffffff1e"),
        # 浮动卡片背景偏暗，text 颜色固定白色，不依赖 is_dark
        "text": QColor(255, 255, 255, 200),
        "text_secondary": QColor(255, 255, 255, 150),
        "card_bg": _qcolor("card_bg", "#00000014", "#ffffff14"),
        "accent": accent,
        "accent_fill": QColor(accent.red(), accent.green(), accent.blue(), 60),
        "warning": _qcolor("accent_warm", "#f59e0b", "#ffc107"),
        "success": success,
        "font_family": ctx.get("font_family", "Microsoft YaHei"),
        "font_size": ctx.get("font_size", 14),
    }
```

### 2.2 关键设计点

| 设计点 | 说明 |
|--------|------|
| `text` / `text_secondary` 固定白色 | 浮动卡片背景偏暗，黑色字看不见 |
| `_qcolor()` 优先用 ctx | ctx 是主程序动态传入的主题色，比 `isDarkTheme()` 更准 |
| `_qcolor()` fallback 按 is_dark 分两套 | 主程序没传 ctx 时仍能工作 |
| `accent.lighter(110)` | 柱状图用浅一档作为填充色，避免太深 |
| `line_fill` alpha = 60 | 折线图区域填充半透明 |

### 2.3 返回字典的键约定

```
chart colors 字典包含以下键（图表 widget 会按这些键取色）：
├─ bar_fill / bar_border    柱状图填充/描边
├─ line / line_fill / point 折线图主线/区域填充/数据点
├─ grid                     网格线
├─ text / text_secondary    文字主色/次色
├─ card_bg                  卡片背景（_StatCard 用）
├─ accent / accent_fill     主色 / 主色填充
├─ warning                  警告色
├─ success                  成功色（与 line 同源）
└─ font_family / font_size  字体族 / 基准字号
```

---

## 三、默认 fallback 配色

> 当 `_context_provider` 返回 None 或抛异常时，用 fallback 配色。
> 关键：**fallback 也必须适合浮动卡片背景**（即固定白色文字）。

### 3.1 `_default_chart_colors()`

```python
def _default_chart_colors() -> dict:
    """图表默认配色（无 context 时使用）

    适用场景：
    - _context_provider 为 None（极少见）
    - _context_provider() 抛异常（防御性编程）
    - 卡片首次显示但还没拉到 ctx

    注意：浮动卡片背景偏暗，text 颜色固定用白色，
    不依赖 isDarkTheme()，避免 qfluentwidgets 主题状态不同步导致黑色字。
    """
    return {
        "bar_fill": QColor(98, 160, 234, 200),
        "bar_border": QColor(98, 160, 234),
        "line": QColor(80, 227, 194),
        "line_fill": QColor(80, 227, 194, 60),
        "point": QColor(80, 227, 194),
        "grid": QColor(255, 255, 255, 30),
        "text": QColor(255, 255, 255, 200),
        "text_secondary": QColor(255, 255, 255, 150),
        "card_bg": QColor(255, 255, 255, 20),
        "accent": QColor(98, 160, 234),
        "accent_fill": QColor(98, 160, 234, 60),
        "warning": QColor(255, 193, 7, 200),
        "success": QColor(80, 227, 194, 200),
        "font_family": "Microsoft YaHei",
        "font_size": 14,
    }
```

### 3.2 `_default_card_colors()`（`_StatCard` 专用，更简单）

```python
def _default_card_colors() -> dict:
    """_StatCard 的默认配色（仅 text 相关）"""
    return {
        "text": QColor(255, 255, 255, 200),
        "text_secondary": QColor(255, 255, 255, 150),
        "font_family": "Microsoft YaHei",
        "font_size": 14,
    }
```

---

## 四、父卡片：拉取并缓存 ctx

### 4.1 标准 `_apply_latest_theme` 模式

```python
class MyCard(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._context_provider = None
        self._chart_style = None  # ← 缓存的 ctx 转换结果
        self._setup_ui()

    def set_context_provider(self, provider):
        self._context_provider = provider

    def show_card(self):
        self._apply_latest_theme()  # ← 卡片显示时拉一次
        self._async_load_data()
        self.setVisible(True)

    def _apply_latest_theme(self):
        """从 ctx 拉取最新主题色，缓存到 self._chart_style"""
        if self._context_provider is None:
            return  # 没 provider，保持 fallback 配色
        try:
            ctx = self._context_provider()
            self._chart_style = _make_chart_colors_from_context(ctx)
        except Exception:
            self._chart_style = None  # 异常时不更新
```

### 4.2 主题色应用到所有子组件

```python
def _apply_chart_style_to_children(self):
    """把 self._chart_style 应用到所有图表/卡片子组件"""
    if self._chart_style is None:
        return
    for card in self.findChildren(_StatCard):
        card.set_colors(self._chart_style)
    for chart in self.findChildren(_BarChartWidget):
        chart.set_colors(self._chart_style)
    for chart in self.findChildren(_LineChartWidget):
        chart.set_colors(self._chart_style)
    for chart in self.findChildren(_ProjectBarWidget):
        chart.set_colors(self._chart_style)
```

### 4.3 何时调用

| 场景 | 调用位置 |
|------|----------|
| 卡片首次显示 | `show_card()` 开头 |
| 主题色变化（动态主题切换） | 主程序通过 `context_provider` 重新调 `show_card` |
| 数据刷新（数据变化但主题不变） | 不需要重新调（缓存的 `_chart_style` 仍有效） |
| 用户手动刷新按钮 | 可选，看是否需要响应主题变化 |

> **推荐**：只在 `show_card` 时调一次，缓存到 `self._chart_style`，后续 `set_colors(self._chart_style)` 直接复用。

---

## 五、子 widget：set_colors 标准接口

### 5.1 `_StatCard.set_colors`

```python
class _StatCard(QFrame):
    def set_colors(self, colors: dict):
        """注入外部配色（来自 context）"""
        self._colors = colors
        self._apply_card_style()  # ← 遍历所有 QLabel 刷新样式

    def _apply_card_style(self):
        # 根据 colors 中的 text / text_secondary / font_family / font_size
        # 重新设置所有 QLabel 的样式（通过 objectName 区分）
        for child in self.findChildren(QLabel):
            obj_name = child.objectName()
            if obj_name == "statValue":
                # 值用 text 色，字号大，加粗
                ...
            elif obj_name == "statSub":
                # 副标题用 text_secondary 色，字号中
                ...
            elif obj_name == "statExtra":
                # 额外小字用 text_secondary 色，字号小，加 opacity
                ...
```

### 5.2 图表 widget.set_colors

```python
class _BarChartWidget(QWidget):
    def set_colors(self, colors: dict):
        """注入外部配色（来自 context）"""
        self._colors = colors
        self.update()  # ← 触发 paintEvent 重绘
```

> 图表只需 `self.update()`，`paintEvent` 会用 `self._colors` 重绘。
> 复杂样式（如 _StatCard）需要 `_apply_card_style()` 手动遍历 setStyleSheet。

---

## 六、常见陷阱

| 陷阱 | 症状 | 修复 |
|------|------|------|
| `text` 跟随 `is_dark` | 浮动卡片背景暗时黑字看不见 | `text` / `text_secondary` 固定白色 |
| `accent` 取了 `.lighter(110)` 但没存原色 | 描边色也变浅 | 同时存 `accent` 原色 + `bar_fill = accent.lighter(110)` |
| ctx.colors 缺某个键 | `KeyError` | 用 `.get(key, fallback)` 而非 `[key]` |
| ctx.colors 值是 `""`（空字符串） | `QColor("")` 失败 | 检查 `if val: return QColor(val)` |
| ctx 含主题色但不含字体 | 子 widget 用默认字体 | ctx.get("font_family", "Microsoft YaHei") |
| 主题色变化后子 widget 没更新 | 显示旧色 | `set_colors` 后立即 `update()` / `_apply_card_style()` |
| 缓存的 `_chart_style` 过期 | 主题切换后无变化 | `show_card` 时重新拉（每次重新进入卡片都拉一次） |

### 6.1 ctx 字段缺失的防御写法

```python
def _make_chart_colors_from_context(ctx: dict) -> dict:
    raw = ctx.get("colors", {}) or {}  # ← None 也安全
    is_dark = ctx.get("is_dark", True) if ctx else True

    def _qcolor(key: str, fallback_light: str, fallback_dark: str) -> QColor:
        val = (raw.get(key) or "")  # ← None 也安全
        if val:
            try:
                return QColor(val)
            except Exception:
                pass  # 颜色字符串无效时 fallback
        return QColor(fallback_dark if is_dark else fallback_light)
    ...
```

### 6.2 主题切换的响应

```python
# 主程序支持动态主题切换时，建议：
# - show_card() 时拉一次 ctx（覆盖 _chart_style）
# - 子 widget 重新 set_colors（清旧色，显新色）

def show_card(self):
    self._apply_latest_theme()  # ← 重新拉 ctx
    self._apply_chart_style_to_children()  # ← 应用到子 widget
    self._async_load_data()
    self.setVisible(True)
```

---

## 七、与主程序 ctx 结构的约定

> UI 插件 ctx 由 `UIPluginRegistry.set_context_provider` 注入。
> 约定包含以下字段（主程序保证存在）：

```python
ctx = {
    "colors": {
        "accent": "#2878dc",         # 主色
        "success": "#00a888",         # 成功色
        "accent_warm": "#f59e0b",     # 警告色
        "border": "#ffffff1e",        # 边框
        "card_bg": "#ffffff14",       # 卡片背景
        # ... 更多键（不保证所有插件都需要）
    },
    "is_dark": True,                 # 当前是否深色主题
    "font_family": "Microsoft YaHei", # 当前字体
    "font_size": 14,                 # 当前基准字号
    # ... 主程序可扩展更多字段
}
```

> 插件只用关心的字段，其他忽略即可。**永远不要假设某个键一定存在**——用 `.get()` + fallback。

## 八、为什么不直接用 qfluentwidgets.isDarkTheme()

| 场景 | `isDarkTheme()` | ctx.colors |
|------|----------------|------------|
| 浮动卡片背景暗 | ❌ 检测不到（用系统主题，不是卡片背景） | ✅ ctx 是主程序给的真实色 |
| 主题切换响应 | ❌ 需要轮询 | ✅ 拉一次就拿到最新 |
| 自定义主题（用户改色） | ❌ 不感知 | ✅ 跟着主程序走 |
| 第三方主题包 | ❌ 检测不到 | ✅ ctx 由主程序统一管理 |

> **结论**：浮动卡片场景**必须用 ctx.colors**，不要依赖 `isDarkTheme()`。