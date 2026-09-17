# UI 插件代码模板 — 独立弹窗（register_window）

> 何时读：插件要一个**可脱离主窗的独立顶级窗口**（工具窗、监控面板、独立编辑器等），
> 或需要"消息卡片按钮 → 打开一个窗口"类联动时。
> 前置依赖：architecture.md（UI 架构总览）。
> 产出：`register_window` 注册 + 窗口内容页 widget。

## 十五、独立弹窗模板（register_window）

> 参考实现：`plugins/ui-slots-demo/`（仓库内置示例插件，2026-09-17 实战沉淀）。
> 最小骨架示例：本技能包 `examples/plugin-window/`（可直接复制改名使用）。
> 适配场景：Git 面板 / 日志查看器 / 独立编辑工具 / 需要离开主窗口独立摆放与并排的界面。

### 15.1 定位：弹窗 vs 浮动卡 vs 工作台页（先选对载体）

| 载体 | 形态 | 何时用 |
|------|------|--------|
| 浮动卡片（`register_floating_card`） | 主窗口内的面板（上下左右/full 容器、工作台 tab） | 默认首选：内容跟对话一起用、不需要离开主窗 |
| 工作台页（`register_workbench_tab`） | 右侧工作台常驻页 | 常驻内容页、跟对话区并列浏览 |
| **独立弹窗（`register_window`）** | **顶级窗口（无边框壳 + 自定义标题栏），可拖到主窗外** | 需要**离开主窗**独立摆放/并排/多屏；工具型界面 |

> ⚠️ 三个载体可复用同一个 widget_class（内容页实现好三约定接口后，
> 浮动卡与弹窗都能装），但注册是两套。

### 15.2 注册 API

```python
registry.register_window(
    plugin_name=PLUGIN_NAME,
    window_id="my-plugin:tool",        # 唯一 ID（open_window/命令联动键，建议带插件前缀）
    widget_class=MyToolPage,           # QWidget 子类（客户区内容）
    title="我的工具窗",                 # 标题栏文本 + 任务栏标题
    icon_path="",                      # 标题栏图标路径，缺省用 DriFox 图标
    width=560, height=430,             # 默认几何
    min_width=380, min_height=300,     # 最小尺寸（0=不限制）
    context_provider=_ctx_provider,    # 可选：上下文拉模型（见 15.4）
    group="",                          # 左侧栏分组覆盖：""（跟随插件归属）| "system" 常驻区 | "custom" 自定义折叠区
    metadata={},
)
```

注册后自动获得：

- **左侧「自定义插件」栏条目**：点击开/关切换（单例：已开 → 关闭；未开 → 打开前置）
- **右键菜单「弹出」**：重新打开/前置（卡片条目同款菜单位）
- **命令 `/<window_id>`**：联动注册（用户插件自动带命名空间前缀）

### 15.3 内容页三约定接口（⚠️ 最关键，漏了会"黑块 + 无数据"）

窗口壳（无边框/标题栏/拖动/缩放/最小化/关闭/主题/生命周期）**全由主程序提供**，
插件只写客户区内容。内容页实现以下三个可选接口（强烈建议全实现）：

```python
class MyToolPage(QWidget):
    def set_context_provider(self, provider):
        """主程序注入上下文拉模型；每次 provider() 返回最新 ctx"""
        self._provider = provider

    def show_card(self):
        """弹窗打开/激活时主程序调用 —— 数据加载与主题应用的唯一入口！
        卡片类插件模板的惯例：__init__ 只建骨架，数据 + 主题样式都放这里"""
        self._apply_theme()
        self._refresh_data()

    def refresh_theme(self):
        """主题切换时主程序调用（显式补刷样式）"""
        self._apply_theme()
```

**踩坑（2026-09-17 git-panel 实测）**：数据加载与主题应用如果只挂在 `show_card()` 上，
而宿主没有调用它 → 内容页呈现"样式全丢（按钮黑块）+ 数据空白"。
主程序侧已修复（弹窗打开自动调用），插件侧要点是：**别在 `__init__` 里做重加载**，
把数据与样式统一收敛到 `show_card()`。

### 15.4 上下文取色规范（ctx colors + fallback）

```python
def _ctx_colors(ctx) -> dict:
    colors = (ctx or {}).get("colors", {}) or {}
    dark = isDarkTheme()   # from qfluentwidgets
    return {
        "text_primary": colors.get("text_primary") or ("rgba(255,255,255,0.9)" if dark else "rgba(0,0,0,0.85)"),
        "text_secondary": colors.get("text_secondary") or ("rgba(255,255,255,0.55)" if dark else "rgba(0,0,0,0.45)"),
        "border": colors.get("border") or ("rgba(255,255,255,0.12)" if dark else "rgba(0,0,0,0.12)"),
        "accent": colors.get("accent") or ("#62a0ea" if dark else "#2878dc"),
    }
```

- 浮动卡槽位的 ctx 由主程序注入（含 `colors`/`is_dark`/`font_family`/`font_size`/
  `project_root`/`session_id` 等，见 architecture.md §四）
- **独立窗口的 provider 由插件自己在 `register_window(context_provider=...)` 提供**，
  推荐直接读主程序主题 token（见 15.5 的 `_ctx_provider`），或返回业务上下文
- 取色永远**先读 ctx、缺了再 fallback**——纯硬编码在深浅主题切换时会失配

### 15.5 完整骨架

`ui/__init__.py`：

```python
# -*- coding: utf-8 -*-
"""<插件> — 独立弹窗示例。"""
import sys
from pathlib import Path

from loguru import logger

_PLUGIN = "<plugin-name>"
_WINDOW_ID = f"{_PLUGIN}:tool"

_ICON_LIGHT = str(Path(__file__).resolve().parent.parent / "icon.svg")
_ICON_DARK = str(Path(__file__).resolve().parent.parent / "icon_dark.svg")


def _ctx_provider() -> dict:
    """窗口上下文（每次调用返回最新值；取色走主程序主题 token）"""
    ctx: dict = {}
    try:
        from app.utils.design_tokens import Colors

        Colors.refresh()
        ctx["colors"] = {
            "text_primary": Colors.TEXT_PRIMARY,
            "text_secondary": Colors.TEXT_SECONDARY,
            "border": Colors.BORDER,
            "accent": Colors.TEXT_ACCENT,
        }
    except Exception:
        pass
    return ctx


def _open_tool_window():
    """打开/前置弹窗，返回内容页实例（供其他入口联动）"""
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        win = UIPluginRegistry.get_instance().open_window(_WINDOW_ID)
    except Exception as e:
        logger.warning(f"[{_PLUGIN}] 打开工具窗失败: {e}")
        return None
    return getattr(win, "_content", None)


def register_ui(registry) -> None:
    prefix = f"ui_plugin_{_PLUGIN.replace('-', '_')}."
    for k in [k for k in sys.modules if k.startswith(prefix)]:
        del sys.modules[k]

    from .tool_page import MyToolPage

    registry.register_window(
        plugin_name=_PLUGIN,
        window_id=_WINDOW_ID,
        widget_class=MyToolPage,
        title="我的工具",
        width=560,
        height=430,
        min_width=380,
        min_height=300,
        context_provider=_ctx_provider,
    )
    logger.info(f"[{_PLUGIN}] 独立弹窗已注册")
```

`ui/tool_page.py`（内容页，三约定接口见 15.3；取色见 15.4）。

### 15.6 联动模式（按钮 → 弹窗 / 浮动卡 → 弹出）

```python
# ① 消息卡片按钮回调里打开弹窗并操作内容页
def _on_card_button(ctx):
    page = _open_tool_window()          # open_window 返回 PluginWindow
    text = ctx.get("card").get_plain_text()   # ctx["card"] 是 MessageCard
    if page is not None:
        page.set_last_message(text)

# ② 代码里任意时机打开
UIPluginRegistry.get_instance().open_window(_WINDOW_ID)

# ③ 浮动卡「弹出为独立窗口」：用户在左侧栏/卡片右键菜单点「弹出」（主程序内置，
#    插件无需写代码）；等价 API：registry.popout_card(card_id)
#    —— 会把卡片的 widget_class + 完整上下文 provider 合成一个临时窗口
```

窗口 API 速查（`UIPluginRegistry`）：

| 方法 | 语义 |
|------|------|
| `open_window(window_id, main_widget=None)` | 单例打开/前置，返回 PluginWindow |
| `toggle_window(window_id)` | 开/关切换（左侧栏点击语义） |
| `hide_window / close_window` | 隐藏（留实例）/ 销毁实例 |
| `get_open_windows()` | `{window_id: PluginWindow}` |
| `popout_card(card_id)` | 把浮动卡弹出为独立窗口 |

### 15.7 生命周期

- 窗口实例随**应用退出**（aboutToQuit 统一销毁）或**插件卸载**销毁；用户手动关窗同销毁实例
- 关闭不通知插件（静默）——需要释放资源的逻辑写在内容页 `__del__`/`destroyed` 里
- 重开 = 重新构造内容页（计数器等内存状态不保留；要持久化用插件文本配置或 SQLite）

### 15.8 验证清单

见 `checklist.md §15`（独立弹窗 / 卡片槽位验证）。
