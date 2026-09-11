---
description: UI（界面组件）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# UI（界面组件）组件开发

> 🟡 **UI 插件开发请调用 `ui-plugin-creator` 技能。**
> 此处仅提供架构参考与双技能桥接信息。

### 文件位置

```
<plugin>/ui/
├── __init__.py          ← 必须定义 register_ui(registry)
└── *.py                 ← widget 模块
```

### register_ui 模板

```python
def register_ui(registry):
    """由 UIPluginRegistry 加载钩子（PluginManager._load_plugin_ui）调用。"""

    # 注册浮动卡片（自动注册同名命令 /<card_id>）
    registry.register_floating_card(
        plugin_name="my-plugin",
        card_id="my-card",
        widget_class=MyCardWidget,     # QWidget 子类，__init__(parent=None)
        container="left",
        title="我的卡片",
        default_visible=False,
    )

    # 注册内容块渲染器
    registry.register_content_renderer(
        plugin_name="my-plugin",
        type_name="my-content",
        render_func=my_render_func,    # (meta_dict, widget) -> HTML 字符串
    )
```

### UI 扩展点总表（UIPluginRegistry 实测 17 个注册方法，两层）

**常用层（8 个）**：

| 扩展点 | 注册方法（关键参数） | 场景 |
|--------|---------------------|------|
| 浮动卡片 | `register_floating_card(plugin_name, card_id, widget_class, container, title, default_visible, context_provider)` | 独立面板/仪表板；自动注册 `/<card_id>` 命令；widget_class 需接受 parent |
| 内容块渲染器 | `register_content_renderer(plugin_name, type_name, render_func, priority)` | 消息流中自定义类型内容的 HTML 渲染；render_func(data, context) -> HTML |
| 消息元素工厂 | `register_message_factory(plugin_name, name, condition_func, factory_func, priority)` | 高级：condition 命中时接管整条消息的构造 |
| 欢迎 tab | `register_welcome_tab(plugin_name, mode_key, label, render_func, priority)` | 欢迎卡片新增 tab；render_func(ctx) -> 完整 HTML |
| 侧边栏项 | `register_sidebar_item(plugin_name, item_id, label, icon_path, group, default_visible, priority, on_click)` | 左侧导航自定义入口；on_click(ctx) |
| 输入框按钮 | `register_input_button(plugin_name, button_id, icon_path, icon_light_path, tooltip, group, priority, on_click, on_right_click)` | 输入区旁自定义按钮 |
| 右键菜单 | `register_context_menu_action(plugin_name, action_id, target, label, action_func, enabled_func, separator_before, priority)` | 定制上下文菜单；action_func(ctx)->bool |
| 设置卡片 | `register_settings_card(plugin_name, card_id, title, widget_class, group, priority, section)` | 设置页自定义卡片 |

**高级层（9 个，一行签名）**：

| 扩展点 | 签名要点 |
|--------|---------|
| 内联标签渲染 | `register_tag_renderer(plugin_name, tag_name, render_func(content, ctx)->HTML, priority)`；纯函数，禁止碰 Qt widget |
| 围栏渲染 | `register_fence_renderer(plugin_name, lang, render_func, streaming_placeholder, priority, assets, bridge_permissions)`；**保留 lang 不可劫持：echarts, mermaid, svg, html, widget** |
| 欢迎动作 | `register_welcome_action(plugin_name, action, handler(content, ctx))`；欢迎 tab HTML 内 `.context-tag` 点击派发，后注册覆盖先注册 |
| @提及提供者 | `register_mention_provider(plugin_name, provider_id, list_func()->[{...}], on_selected)`；条目渲染在 @ 卡片顶部 |
| 标题栏 tab | `register_titlebar_tab(plugin_name, tab_id, label, icon_path, on_click, priority)`；icon 无主题感知，建议纯文字 label |
| 工作台页签 | `register_workbench_tab(plugin_name, page_id, label, widget_class, priority)`；同 page_id 高优先级覆盖 |
| 工作区页面 | `register_workspace_page(plugin_name, page_id, title, widget_class, icon_path, icon_light_path, order_hint)`；icon_light_path 双主题 |
| 槽位条目 | `register_slot_entry(region_id, entry_id, plugin_name, priority, payload)`；宿主须先 `declare_region` 声明区域 |
| UI 模块 | `register_ui_module(module_id, factory, plugin_name, priority)`；factory 延迟构造，多实现并存胜者=最高 priority，**覆盖系统实现须 priority≥100** |

### 参考

- 浮动卡片案例：`plugins/context-usage-stats/`、`plugins/file-tree/`
- 完整 UI 插件：`plugins/plugin-marketplace/`
- 欢迎页 tab 案例：`plugins/welcome_changelog/`
- **开发 UI 插件**：调用 `ui-plugin-creator` 技能（最小骨架见其 `examples/`）

---
