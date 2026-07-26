# Tab 模式 UI 插件列表设计

日期：2026-07-26

## 目标

Tab 模式下不再显示共享的 `UIPluginEdgeLauncher`。在左侧 `TabPanel` 的“新建标签页”按钮上方直接显示所有可用 UI 插件，点击插件后在当前活动 Tab 中打开对应浮动卡片。多窗口模式保持现有边缘入口行为不变。

## 设计

### TabPanel 插件区域

`TabPanel` 新增一个顶部 UI 插件区域，位于“新建标签页”按钮之前。区域从 `UIPluginRegistry.get_floating_cards()` 读取插件卡片信息，按标题稳定排序，并为每个卡片创建一个可点击按钮。按钮显示插件标题；如果插件提供图标，则显示对应主题图标。

列表没有插件时隐藏整个区域，避免占用空白空间；有插件时显示标题和列表按钮。按钮样式沿用 TabPanel 当前主题颜色、字体缩放和悬停状态。

### 打开插件

点击插件按钮时，`TabPanel` 通过父级查找 `TabManagerWindow`，获取 `get_current_window()` 返回的活动聊天窗口，并调用：

```python
UIPluginRegistry.get_instance().toggle_floating_card(
    card_id,
    main_widget=current_window,
)
```

Tab 模式始终至少存在一个打开的标签，因此不设计无当前窗口的分支。

### 生命周期与刷新

- `TabManagerWindow` 不再创建、维护或刷新共享 `UIPluginEdgeLauncher`。
- `OpenAIChatToolWindow` 中的 `_ui_plugin_edge_launcher` 及其 resize、主题刷新、热重载逻辑保留，用于多窗口模式。
- Tab 模式 UI 插件列表在初始化完成后刷新；UI 插件热重载后刷新；主题/字号变更时刷新样式和图标。
- 插件列表按钮只负责触发注册表操作，不复制浮动卡片生命周期逻辑。

## 修改范围

- `app/widgets/tab_panel.py`：新增插件列表区域、插件按钮、刷新和点击处理。
- `app/widgets/tab_manager_window.py`：移除 Tab 模式共享 EdgeLauncher 的创建、显示、隐藏、刷新及主题联动代码；把插件列表刷新转发给 TabPanel。
- `docs/superpowers/specs/2026-07-26-tab-ui-plugin-list-design.md`：本设计文档。

不删除 `app/widgets/ui_plugin_edge_launcher.py`，因为多窗口模式仍然依赖它。不修改当前工作树中与本任务无关的已有变更。

## 验证标准

1. Tab 模式左侧面板中，“新建标签页”按钮上方显示所有已注册 UI 插件。
2. 点击任一插件后，卡片出现在当前活动 Tab 对应窗口中。
3. 切换 Tab 后点击插件，卡片操作目标随当前 Tab 改变。
4. UI 插件热重载后列表内容同步更新。
5. 主题/字号变化后列表样式和图标正常刷新。
6. 多窗口模式仍显示并使用原有边缘插件入口。
7. 相关 Python 文件通过语法检查。
