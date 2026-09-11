# UI 扩展槽位与通用挂载模型（Phase E 一期）

> DriFox UI 灵活性三层模型的底层——条目级（Region/SlotEntry）。本文件覆盖 Phase E Region 通用挂载模型、UIEventBus、IWindowHost 协议，以及 Phase D 4 类槽位的底层迁移说明。

---

## 1. UI 注册 API 总览（全部真实签名）

`ui/__init__.py` 暴露 `def register_ui(registry)`，在加载时注册组件；
**签名以本节为准**（`app/plugins/registries/ui_plugin_registry.py`），按文档写错会直接 `TypeError`。

### 1.1 渲染器 / 工厂（内容层）

```python
def register_ui(registry):
    # 自定义内容块渲染器（content 中 custom_type 字段的分发目标）
    registry.register_content_renderer(
        plugin_name, type_name, render_func,
        priority=0, metadata=None,
    )  # render_func: (data: dict, context) -> str(HTML)

    # 消息文本内联标签渲染器（LLM 输出中的 <tag>...</tag> 块 → 卡片 HTML）
    registry.register_tag_renderer(
        plugin_name, tag_name, render_func,
        priority=0, metadata=None,
    )  # render_func: (content: str, ctx: dict) -> str(HTML)；ctx 含 tag/completed/compact

    # 消息正文 fence 代码块渲染器（``<lang>`` 的渲染方式，内置 echarts/mermaid/svg/html 不可劫持）
    registry.register_fence_renderer(
        plugin_name, lang, render_func,
        streaming_placeholder=None, priority=0,
        assets=None, bridge_permissions=None, metadata=None,
    )  # render_func: (code: str, ctx: dict) -> str(HTML 片段)，纯函数（后台线程调用）
       # assets: {"js": "ui/assets/fence/x.js", "css": ...}（相对插件根，宿主按需注入）
       # bridge_permissions: 桥权限声明，取值见 FENCE_BRIDGE_PERMISSIONS（如 "theme"/"sendPrompt"/"storage"）

    # 消息元素工厂（condition 命中才接管该消息的 widget 构建）
    registry.register_message_factory(
        plugin_name, name, condition_func, factory_func,
        priority=0,
    )  # condition_func: (message) -> bool；factory_func: (message, parent) -> QWidget

    # 欢迎卡片插件 tab
    registry.register_welcome_tab(
        plugin_name, mode_key, label, render_func,
        priority=0, metadata=None,
    )  # mode_key 同时是 welcome mode 值，避开系统内置 sessions/projects

    # 欢迎卡片点击动作（.context-tag 的 data-type 派发）
    registry.register_welcome_action(
        plugin_name, action, handler,
        metadata=None,
    )  # handler: (content: str, ctx: dict) -> None；建议动作名带插件前缀

    # @ 提及条目提供者（@ 卡片顶部渲染的条目源）
    registry.register_mention_provider(
        plugin_name, provider_id, list_func,
        on_selected=None, metadata=None,
    )  # list_func: () -> List[dict]，同步主线程调用；on_selected: (entry, ctx) -> None
```

### 1.2 浮动卡 / 常驻元素（控件层）

```python
def register_ui(registry):
    # 浮动卡片（container: "top"|"bottom"|"left"|"right"|"full"；Tab 窗口级容器对应方位）
    registry.register_floating_card(
        plugin_name, card_id, widget_class, container,
        title="", default_visible=False,
        metadata=None, context_provider=None,
    )  # 自动注册命令 /{card_id}；context_provider 可选覆盖全局上下文

    # 侧边栏插件项（独立扩展点，与浮动卡解耦）
    registry.register_sidebar_item(
        plugin_name, item_id, label, icon_path="",
        group="custom", default_visible=True, priority=0,
        on_click=None, metadata=None,
    )

    # 输入区工具栏按钮（position: "start"|"before:<id>"|"after:<id>"|"end"）
    registry.register_input_button(
        plugin_name, button_id, icon_path="", icon_light_path="", tooltip="",
        group="plugin", priority=0,
        on_click=None, on_right_click=None, metadata=None, position="end",
    )  # icon_path 深色 / icon_light_path 浅色（缺省回退深色），主题切换自动刷新

    # 右键菜单项（target: "message_card"|"tab"|"input_area"；target 是第 3 个位置参数！）
    registry.register_context_menu_action(
        plugin_name, action_id, target, label, action_func,
        enabled_func=None, separator_before=False, priority=0, metadata=None,
    )  # action_func: (ctx) -> bool（返回 True 表示已处理）；enabled_func: (ctx) -> bool 控制灰显

    # 设置面板卡片（section: "plugins"|"llm"|"common"|"appearance"|"update"）
    registry.register_settings_card(
        plugin_name, card_id, title, widget_class,
        group="plugin", priority=0, metadata=None, section="plugins",
    )

    # 标题栏常驻 tab（无 × 关闭钮；点击走 on_click 回调自展示，主程序不接管内容区）
    registry.register_titlebar_tab(
        plugin_name, tab_id, label, icon_path="",
        on_click=None, priority=0, metadata=None,
    )

    # 右侧工作台 page（WorkbenchPanel 页签：按 order_hint 排序，注册即得 /{page_id} 命令）
    registry.register_workbench_tab(
        plugin_name, page_id, label, widget_class,
        priority=0, metadata=None,  # metadata 可带 order_hint / default_landing
    )

    # 工作区页面（Phase G，见 ui-workspace.md）
    registry.register_workspace_page(
        plugin_name, page_id, title, widget_class,
        icon_path="", icon_light_path="", order_hint=500, metadata=None,
    )
```

详见 [`docs/plugin-architecture.md`](../plugin-architecture.md) 466-477 行。

> **标题栏 tab 两类形态**：
> - **常驻**（`register_titlebar_tab`）：始终显示在标题栏 tab 区（「聊天」右侧），不可关闭，点击触发插件回调。
> - **非常驻**（`register_floating_card(container="full")`）：卡片打开时动态出现在标题栏（带 × 关闭钮），关闭即从标题栏移除；点击 tab 切换覆盖层显示。

> **工作台 tab（`register_workbench_tab`）**：注册到右侧工作台（WorkbenchPanel）的页签条。★ 面板**零保留槽位、零 page_id 语义**：页序 = `(metadata["order_hint"], 注册序)`，默认落点 = `metadata["default_landing"]` 标记页（缺省顺序第一页），一个页都没注册时空态页。宿主在 `refresh_workbench` 时调 `panel.sync_plugin_pages(tabs)` reconcile；页签一律按 **tab_id** 定位（`set_current_tab_by_id` / `current_tab_id`），宿主不得假设 index。**注册即联动注册 `/{page_id}` 命令**。数据由页面**自拉**：面板只发无参 `refresh_current_page_data()`，页面实现可选协议 `refresh_data()`（从 `context` / 活跃窗口取数）。参考实现：`plugins/artifacts-manager/ui/artifacts_page.py`、`plugins/worktree-manager/ui/worktree_page.py`、`plugins/history-manager/ui/history_page.py`。

### UI 扩展点 → 自动命令（联动矩阵）

`register_*` 时自动登记一条系统命令到 `UIPluginRegistry` 命令账本
（`_ui_commands`），并在 `builtin_commands.register_all_commands()` 清空命令表后由
`re_register_all_commands()` **全量重放**恢复；插件卸载时按 `owner` 批量注销。
命令名优先短 id，与其它插件/系统命令重名时自动加 `<plugin>:` 前缀。

| 扩展点 | 命令语义 | 备注 |
|---|---|---|
| `register_floating_card` | 打开浮动卡片 | 同名系统命令优先（不抢占） |
| `register_workbench_tab` | 展开工作台并定位该页 | `/worktree-manager`、`/artifacts-manager`、`/history-manager`（page_id 即插件名） |
| `register_workspace_page` | 打开工作区页面 | 由 `WorkspacePageHost` 经账本登记 |
| `register_sidebar_item` | 等价点击侧边栏项（派发 `on_click(context)`） | 无回调时不注册 |
| `register_input_button` | 等价点击输入区按钮（派发 `on_click(context)`） | 无回调时不注册 |
| `register_titlebar_tab` | 等价点击标题栏常驻 tab（`on_click()`） | 无回调时不注册 |

**刻意不联动命令**的扩展点（非独立可触发界面，或需上下文）：
`register_context_menu_action`（依赖右键目标上下文）、`register_settings_card`
（设置面板内的分区卡，属导航而非独立界面）、`register_welcome_tab` /
`register_welcome_action`（欢迎卡片内部 tab / HTML 点击动作，需欢迎卡片在场
且携带内容参数）、`register_content_renderer` / `register_tag_renderer` /
`register_fence_renderer` / `register_message_factory` / `register_mention_provider` /
`register_ui_module`（渲染/装配类，无用户可触发界面）。

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

# 实际实现是模块级字符串常量（非 class），允许宿主自定义扩展值：
#   MENU / LIST_ITEM / TOOLBAR_BUTTON / PANEL / CONTENT
# VALID_REGION_KINDS = frozenset({...}) 只作合法性校验。
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
EV_WELCOME_TAB_REFRESHED = "welcome_tab_refreshed"
    # payload: mode_key (str), plugin_name (str), window_id (str,可选)
    # 欢迎卡片 tab 数据已更新（异步 fetcher 完成/数据源刷新），主程序对指定 mode_key 重渲染
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
    "my-plugin", "act-1", "input_area", "增强粘贴",
    lambda ctx: True,  # action_func，(ctx) -> bool；target 是第 3 个位置参数
)  # target: "message_card" | "tab" | "input_area"
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