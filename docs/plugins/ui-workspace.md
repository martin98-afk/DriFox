# UI 工作区扩展：停靠区多卡堆叠 + WorkspacePage

> DriFox UI 灵活性三层模型的顶层。条目级（[ui_slots](./plugin-architecture.md)）/ 模块级（[ui_modules](./plugin-architecture.md)）已在前两期落地，本期新增**页面级**：插件可注册完整主页面（非对话形态的 Tab 页）。

---

## 1. 三层模型终图

```
┌─────────────────────────────────────────────────────────────┐
│ 页面级（Phase G）：WorkspacePage — 插件提供完整主页面        │
│ 适用：看板 / 仪表盘 / 数据库浏览器 / 独立工具页             │
├─────────────────────────────────────────────────────────────┤
│ 模块级（Phase F）：UIModule — 插件替换某区域整体实现        │
│ 适用：自定义消息卡片 / 自定义输入区 / 自定义侧边栏           │
├─────────────────────────────────────────────────────────────┤
│ 条目级（Phase E）：SlotEntry — 插件往某区域加一个条目       │
│ 适用：floating_card / sidebar item / input button / menu     │
└─────────────────────────────────────────────────────────────┘
```

### 决策树：何时用哪一层

```
需要往侧边栏/输入区/菜单加一条？           → 条目级（register_sidebar_item 等）
需要替换某区域（消息卡片/输入区）整套实现？ → 模块级（register_ui_module）
需要提供完整的非对话主页面（看板/仪表盘）？  → 页面级（register_workspace_page）✓ 本期
```

| 场景 | 推荐层级 | 说明 |
|---|---|---|
| 加一个浮动设置卡片 | 条目级 | `register_floating_card` |
| 在侧边栏加一个图标入口 | 条目级 | `register_sidebar_item` |
| 给消息卡片加自定义按钮 | 条目级 | `register_input_button` |
| 替换整个消息渲染逻辑 | 模块级 | `register_ui_module("message_card", ...)` |
| 做一个独立的"看板"页面 | **页面级** | `register_workspace_page` |
| 做一个"数据库浏览器"主页面 | **页面级** | `register_workspace_page` |

---

## 2. 停靠区多卡堆叠（dock stacking）

### 声明方式

插件注册浮动卡时声明 `metadata={"stack": True}`，卡片 widget 实例化后自动打 `stackInDock` 属性：

```python
def register_ui(registry):
    registry.register_floating_card(
        "my-plugin", "file-tree", FileTreeWidget,
        container=ContainerType.LEFT,  # 仅 LEFT/RIGHT 生效
        title="文件树",
        metadata={"stack": True},      # ← 关键：声明停靠区堆叠
    )
```

未声明 `stack: True` 的卡片保持旧行为（同侧互斥单卡），零侵入。

### 行为差异

| 维度 | 旧（单卡互斥） | 新（多卡堆叠） |
|---|---|---|
| 容器数据模型 | `visible_cards[ct] = Optional[str]` | `visible_cards[ct] = List[str]` |
| 当前激活 | 隐含（唯一可见即激活） | `active_cards[ct] = Optional[str]` |
| TOP/BOTTOM 容器 | 互斥 | **零改动**（系统卡约束保留） |
| LEFT/RIGHT 容器 | 互斥 | 多卡共存 + Pivot 切换 |
| 与非堆叠卡互斥 | — | 非堆叠卡 show 进 LEFT/RIGHT 时清空可见列表回退旧行为 |

### Pivot 交互

`CardStackContainer`（`app/widgets/cards/card_stack_container.py`）使用 qfluentwidgets Pivot + QStackedWidget 渲染多卡：

- 默认 order = 注册顺序
- 点击 Pivot 切换激活卡，触发 `set_active_card`（仅状态切换，无 show/hide 回调风暴）
- 空栈自动隐藏（与单卡容器折叠行为一致）
- Pivot 配色全走 `design_tokens.py Colors.*`，跟随主题切换

> ⚠️ **实验特性**：Pivot 样式尚未经过设计评审，主题色全走 Colors 体系可后期调整。

### 卸载与热重载

- 卡片 widget 销毁 → 容器自动从可见列表移除 → 栈容器同步 sync
- 插件热重载（unload → load）后已打开标签页的卡片视图按新 widget_class 重建恢复（避免"必须重开标签页"）
- TOP/BOTTOM 系统卡互斥逻辑零改动（Global Constraints 第一条）

---

## 3. WorkspacePage API

### 注册

```python
from app.plugins.contracts.ui_page import WorkspacePageInfo

def register_ui(registry):
    registry.register_workspace_page(
        plugin_name="my-plugin",
        page_id="kanban",            # 唯一 ID（同时作为命令名）
        title="看板",                 # 侧边栏入口文本
        widget_class=KanbanPage,     # 页面 widget 类
        icon_path="icons/kanban.svg", # 深色主题图标（可选）
        icon_light_path="icons/kanban-light.svg",  # 浅色主题图标（可选）
        order_hint=500,              # 排序权重（小者在前；系统 <100，插件默认 500）
        metadata={"hide_sidebar": False},  # True 时不进入侧边栏入口
    )
```

### Widget 构造约定

```python
class KanbanPage(QWidget):
    def __init__(self, parent=None, context=None):
        super().__init__(parent)
        # context 字段：
        #   - window_id: str       当前窗口 ID（"__global__" 或具体 ID）
        #   - project_root: Path   当前项目根目录（可选）
        #   - theme: str           "dark" | "light"
        #   - services: dict       跨插件服务查找表（ctx 等价物）
```

### 生命周期

- **懒创建**：`show_page(page_id)` 首访才实例化 widget，二次访问复用
- **激活**：`WorkspacePageHost.show_page` 切 `_content_area` 当前索引
- **入口**：自动注册侧边栏入口（`group="custom"`）+ `/<plugin_name>:<page_id>` FUNCTION 命令
- **卸载清理**：`UIPluginRegistry.unload_plugin(plugin_name)` 清理注册 + `WorkspacePageHost.teardown_plugin` 销毁 widget + 移除 content_area 占位 + 注销命令

### 排序规则

`order_hint` 升序 → 同 `order_hint` 按注册顺序。例如：

```python
registry.register_workspace_page("system", "chat", "对话", ChatPage, order_hint=10)
registry.register_workspace_page("plugin-a", "kanban", "看板", KanbanPage, order_hint=500)
registry.register_workspace_page("plugin-b", "docs", "文档", DocsPage, order_hint=500)
```

输出顺序：`chat, kanban, docs`（系统在前，插件按注册序 tiebreak）。

---

## 4. 完整示例插件

`tests/plugins/e2e/workspace-demo/` 提供验收夹具（cp 到 `~/.drifox/plugins/` 手动点检）。

### 目录结构

```
workspace-demo/
├── .drifox-plugin/
│   └── plugin.json         # manifest
└── ui/
    └── __init__.py         # register_ui 入口
```

### `plugin.json`

```json
{
  "name": "workspace-demo",
  "version": "0.1.0",
  "entry": "ui"
}
```

### `ui/__init__.py`

```python
# -*- coding: utf-8 -*-
from PyQt5.QtWidgets import QLabel, QVBoxLayout, QWidget


class KanbanPage(QWidget):
    def __init__(self, parent=None, context=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        wid = context.get("window_id") if context else "?"
        lay.addWidget(QLabel(f"看板页面（window={wid}）", self))


def register_ui(registry):
    registry.register_workspace_page(
        "workspace-demo", "kanban", "看板", KanbanPage, order_hint=10, icon_path=""
    )
```

### 手动点检流程

1. `cp -r tests/plugins/e2e/workspace-demo ~/.drifox/plugins/`
2. 启动 DriFox → 侧边栏出现「看板」入口
3. 点击 → `content_area` 切到看板页
4. 命令面板输入 `/workspace-demo:kanban` → 直达
5. `rm -rf ~/.drifox/plugins/workspace-demo` → 热卸载 → 入口与页面消失，回退对话页

---

## 5. FAQ

### 何时用 `register_floating_card` vs `register_workspace_page`？

| 场景 | 用 | 理由 |
|---|---|---|
| 设置面板 / 服务商编辑 / 单卡弹窗 | `floating_card` | 临时面板，按需 show/hide |
| 长期显示的文件树 / 历史记录 / 标签管理 | `floating_card` + `stack` | 需保持停靠区长期可见，堆叠与同类卡共存 |
| 看板 / 仪表盘 / 数据库浏览器 | `workspace_page` | 独立主页面，需全屏切换 |
| Markdown 渲染 / 自定义消息块 | `content_renderer` | 内嵌到对话流 |

### 能否在同一插件里同时注册 floating_card 和 workspace_page？

可以，两者独立。floating_card 挂停靠区容器，workspace_page 挂 content_area，互不干扰。

### WorkspacePage 能访问对话历史吗？

通过 `context.services` 查找所需服务键（具体服务列表见插件架构文档）。无对话历史专用接口，需要插件自行设计存储。

### Pivot 主题色怎么调？

`CardStackContainer` 样式全走 `design_tokens.py Colors.*`。如需自定义，修改对应 Colors 字段即可全局生效（无需改 pivot 组件）。

### TOP/BOTTOM 容器能堆叠吗？

不能（设计约束）。系统卡互斥逻辑零改动是三期强约束（`docs/superpowers/plans/2026-08-23-ui-dock-workspace-phase3.md` Global Constraints 第一条）。如需多卡堆叠在 TOP/BOTTOM，改用 LEFT/RIGHT 容器。