# UI 扩展槽位与通用挂载模型（Phase E 一期）

> DriFox UI 灵活性三层模型的底层——条目级（Region/SlotEntry）。本文件覆盖 Phase E Region 通用挂载模型、UIEventBus、IWindowHost 协议，以及 Phase D 4 类槽位的底层迁移说明。

---

## 1. 8 类既有扩展点 API

```python
def register_ui(registry):
    # Phase D 原始 4 类 + Phase E 增强
    registry.register_floating_card(plugin_name, card_id, widget_class, container, title, ...)
    registry.register_content_renderer(plugin_name, type_name, render_func)
    registry.register_message_factory(plugin_name, name, condition_func, factory_func)
    registry.register_welcome_tab(plugin_name, mode_key, label, render_func)

    registry.register_sidebar_item(plugin_name, item_id, label, group="custom", priority=0, on_click=..., metadata=...)
    registry.register_input_button(plugin_name, button_id, icon_path, tooltip, group="plugin", priority=0, on_click=..., on_right_click=..., position="end", metadata=...)
    registry.register_context_menu_action(plugin_name, action_id, label, on_click, target="message_card", group="plugin", priority=0)
    registry.register_settings_card(plugin_name, card_id, title, widget_class, section="plugins", icon="", priority=0)

    # 标题栏常驻 tab（无 × 关闭钮；点击走 on_click 回调自展示，主程序不接管内容区）
    registry.register_titlebar_tab(plugin_name, tab_id, label, icon_path="", on_click=..., priority=0, metadata=...)

    # 右侧工作台 tab（WorkbenchPanel 页签条：产物 / 记忆 之后追加）
    registry.register_workbench_tab(plugin_name, page_id, label, widget_class, priority=0, metadata=...)
```

详见 [`docs/plugin-architecture.md`](../plugin-architecture.md) 466-477 行。

> **标题栏 tab 两类形态**：
> - **常驻**（`register_titlebar_tab`）：始终显示在标题栏 tab 区（「聊天」右侧），不可关闭，点击触发插件回调。
> - **非常驻**（`register_floating_card(container="full")`）：卡片打开时动态出现在标题栏（带 × 关闭钮），关闭即从标题栏移除；点击 tab 切换覆盖层显示。

> **工作台 tab（`register_workbench_tab`）**：注册到右侧工作台浮层（WorkbenchPanel）的页签条，自动出现在「产物」「记忆」之后；宿主在 `refresh_workbench` 时调用 `panel.sync_plugin_pages(tabs)` reconcile（签名不变则跳过重建）。同 page_id 高优先级覆盖低优先级，插件卸载时自动注销。系统插件 `plugins/system/ui/_artifacts_page.py` 提供 `SystemArtifactsPage` 作为参考实现，演示如何通过 `context["backend"]` / `context["session_id"]` / `context["diff_requested_callback"]` 从宿主拉取数据与触发回调。

---

## 2. Region 通用挂载模型（Phase E）

新增 UI 槽位时不再改注册表——宿主声明区域，插件向任意区域挂条目。

### 契约

```python
# app/plugins/contracts/ui_slots.py
@dataclass(frozen=True)
class SlotEntry:
    entry_id: str
    plugin_name: str
    region_id: str
    priority: int = 0
    payload: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)

class RegionKind:
    MENU = "menu"           # 右键菜单
    LIST_ITEM = "list_item" # 列表项（侧边栏）
    TOOLBAR_BUTTON = "toolbar_button"
    PANEL = "panel"         # 设置面板
    CONTENT = "content"
```

### API

```python
registry.declare_region(region_id, kind, description="")
registry.register_slot_entry(region_id, entry_id, plugin_name, priority=0, payload=None, metadata=None)
registry.get_region_entries(region_id) -> List[SlotEntry]   # priority 降序 → 注册序
registry.get_region_entry(region_id, entry_id) -> Optional[SlotEntry]
```

### Region ID 命名约定

| 命名 | 类型 | 示例 |
|---|---|---|
| 简单名 | 主区域 | `sidebar` / `toolbar:input` |
| `menu:<target>` | 右键菜单 | `menu:message_card` / `menu:tab` / `menu:input_area` |
| `toolbar:<area>` | 工具栏 | `toolbar:input` |
| `settings:<section>` | 设置面板 | `settings:plugins` / `settings:llm` / `settings:common` / `settings:appearance` / `settings:update` |

### 默认声明区域（主程序内置）

`UIPluginRegistry.__init__` 自动声明：

```
sidebar              LIST_ITEM      左侧边栏插件项
toolbar:input        TOOLBAR_BUTTON 输入区工具栏按钮
menu:message_card    MENU           消息卡片右键菜单
menu:tab             MENU           Tab 标签右键菜单
menu:input_area      MENU           输入框右键菜单
settings:plugins     PANEL          设置面板插件分区
settings:llm         PANEL          设置面板大模型分区插件卡
settings:common      PANEL          设置面板通用分区插件卡
settings:appearance  PANEL          设置面板外观分区插件卡
settings:update      PANEL          设置面板更新分区插件卡
```

### Payload 约定

| Region Kind | Payload 类型 | 说明 |
|---|---|---|
| `LIST_ITEM` | `SidebarItemInfo` | 侧边栏插件项注册信息 |
| `TOOLBAR_BUTTON` | `InputButtonInfo` | 输入区工具栏按钮 |
| `MENU` | `ContextMenuActionInfo` | 右键菜单项 |
| `PANEL` | `SettingsCardInfo` | 设置面板卡片 |

### 同 entry_id 高 priority 覆盖

```python
registry.register_slot_entry("sidebar", "my-item", "plugin-a", priority=1)
registry.register_slot_entry("sidebar", "my-item", "plugin-b", priority=10)
# get_region_entries("sidebar") 仅返回 plugin-b 的高优项
```

### 卸载清理

`UIPluginRegistry.unload_plugin(plugin_name)` 自动清理该插件在所有 region 的条目（Phase E 单源化）。

---

## 3. UIEventBus（Phase E 事件总线）

插件订阅主程序 UI 事件，无需在 unload_ui 手动退订——`unload_plugin` 自动清理。

### 事件常量

```python
from app.core.ui_event_bus import UIEventBus, EV_THEME_CHANGED, EV_TAB_SWITCHED, EV_CARD_VISIBILITY_CHANGED, EV_WINDOW_ACTIVATED

EV_THEME_CHANGED = "theme_changed"
    # payload: theme_id (str), theme_name (str), is_dark (bool)
EV_TAB_SWITCHED = "tab_switched"
    # payload: tab_index (int), window_id (str)
EV_CARD_VISIBILITY_CHANGED = "card_visibility_changed"
    # payload: card_id (str), window_id (str), visible (bool)
EV_WINDOW_ACTIVATED = "window_activated"
    # payload: window_id (str)
```

### 订阅示例

```python
def register_ui(registry):
    bus = UIEventBus.get_instance()

    def on_theme_changed(payload):
        # payload: {"theme_id": "midnight", "theme_name": "午夜", "is_dark": True}
        # 重新应用自定义 UI 主题色
        ...

    bus.subscribe(EV_THEME_CHANGED, on_theme_changed, plugin_name="my-plugin")
    # 卸载自动退订——unload_plugin("my-plugin") 调用 unsubscribe_plugin
```

### 异常隔离

单个订阅回调抛异常不影响其他订阅者（记 warning 日志）。

---

## 4. IWindowHost Protocol（Phase E 显式契约）

UI 插件宿主的显式契约——收敛鸭子属性耦合，新宿主无需手抄 `_card_manager` / `_window_id` 私有属性名。

### 协议

```python
# app/plugins/contracts/ui_host.py
@runtime_checkable
class IWindowHost(Protocol):
    @property
    def window_id(self) -> str: ...

    @property
    def card_manager(self) -> "CardManager": ...

    def as_ui_host(self) -> "IWindowHost":
        """自描述入口 — registry 探测宿主的优先路径（返回 self）"""
        ...

def is_ui_host(obj: Any) -> bool:
    """runtime 探测：对象是否实现 IWindowHost 协议"""
    if obj is None:
        return False
    return all(hasattr(obj, attr) for attr in ("window_id", "card_manager", "as_ui_host"))
```

### 新宿主接入

```python
class MyCustomWindow(QWidget):
    def __init__(self):
        super().__init__()
        self._card_manager = CardManager.get_instance()
        self._window_id = "my-window-id"

    @property
    def window_id(self) -> str:
        return self._window_id

    @property
    def card_manager(self) -> "CardManager":
        return self._card_manager

    def as_ui_host(self) -> "IWindowHost":
        return self  # 自描述
```

`UIPluginRegistry._resolve_global_host()` 优先探测 `as_ui_host()`，鸭子属性路径仅作 legacy 兜底。

---

## 5. 增强参数

### `position`（input_button）

```python
registry.register_input_button(
    "my-plugin", "btn-1", icon_path="...", tooltip="...",
    position="end",  # "start" | "before:<button_id>" | "after:<button_id>" | "end"
)
```

- `start`：capsule 首位
- `before:memory`：锚定记忆按钮左侧（锚点缺失降级末尾）
- `after:history`：锚定历史按钮右侧
- `end`：默认末尾

锚点匹配 `_input_card` 内胶囊布局子控件 objectName（系统按钮已设 `memory` / `history` / `new_session`）。

### `section`（settings_card）

```python
registry.register_settings_card(
    "my-plugin", "my-card", "我的卡片", MyCardWidget,
    section="plugins",  # "plugins" | "llm" | "common" | "appearance" | "update"
)
```

挂载到对应设置面板分区。

### `target`（context_menu_action）

```python
registry.register_context_menu_action(
    "my-plugin", "act-1", "增强粘贴",
    on_click=lambda ctx: ...,
    target="input_area",  # "message_card" | "tab" | "input_area"
)
```

新增 `input_area` target：输入框右键菜单。

---

## 6. 二/三期路线图

| 计划 | 层级 | 状态 |
|---|---|---|
| Phase E（一期） | 条目级 Region/SlotEntry | ✅ 已落地 |
| Phase F（二期） | 模块级 UIModule（替换整个区域实现） | ✅ chat_area 已完整搬迁；其余 4 模块瘦版占位，待 Phase 2 Task 4-8 完整搬迁 |
| Phase G（三期） | 页面级 WorkspacePage | ✅ 已落地 |

详见：
- [`docs/plugins/ui-modules.md`](./ui-modules.md)（模块级，待 T10 完成）
- [`docs/plugins/ui-workspace.md`](./ui-workspace.md)（页面级）