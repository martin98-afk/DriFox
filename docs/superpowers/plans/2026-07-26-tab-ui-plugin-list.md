# Tab UI Plugin List Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Tab 模式左侧面板的“新建标签页”按钮上方显示可直接点击的 UI 插件列表，并将插件操作绑定到当前活动 Tab，同时保持多窗口边缘入口不变。

**Architecture:** `TabPanel` 负责展示和刷新插件按钮，点击时向父级 `TabManagerWindow` 获取当前窗口并调用 `UIPluginRegistry.toggle_floating_card()`。`TabManagerWindow` 删除仅属于 Tab 模式的共享 EdgeLauncher 生命周期代码，并把插件列表刷新委托给 `TabPanel`；独立窗口中的 EdgeLauncher 代码不变。

**Tech Stack:** Python 3.14+, PyQt5, qfluentwidgets, UIPluginRegistry, pytest/ruff。

---

### Task 1: 在 TabPanel 增加插件列表区域

**Files:**
- Modify: `app/widgets/tab_panel.py:TabPanel._setup_ui`、`TabPanel` 样式刷新相关方法

- [ ] **Step 1: 保留现有未提交 TabPanel 修改，并定位布局插入点**

  现有 `_setup_ui()` 的顺序是顶部新建按钮、Tab 滚动列表、分隔线、设置按钮。只在顶部新建按钮之前插入插件区域，不重写相邻 TabItem 或拖拽逻辑。

- [ ] **Step 2: 添加插件区域的状态和布局**

  在 `TabPanel.__init__` 中增加插件区域引用和卡片元数据缓存，例如：

  ```python
  self._plugin_section = None
  self._plugin_layout = None
  self._plugin_buttons = []
  self._plugin_infos = []
  ```

  在 `_setup_ui()` 中创建 `QWidget`、标题 `CaptionLabel("UI 插件")` 和纵向布局，并将其插入主布局且位于 `top_bar` 之前。插件区域设置为 `setVisible(False)`，避免注册表尚未加载时显示空白区域。

- [ ] **Step 3: 实现插件列表刷新**

  增加 `refresh_ui_plugins()`，从 `UIPluginRegistry.get_instance().get_floating_cards()` 读取卡片；逐项安全读取 `title`、`plugin_name`，按标题小写排序；清理旧按钮后创建新的 `TransparentPushButton`。

  每个按钮保存 `card_id`，显示插件标题；用户插件标题未带插件名时可显示为 `标题 · 插件名`，与现有 EdgeLauncher 菜单保持一致。按钮点击使用默认参数捕获 `card_id`，避免循环变量晚绑定：

  ```python
  button.clicked.connect(lambda checked=False, cid=card_id: self._on_ui_plugin_clicked(cid))
  ```

  没有插件时隐藏 `_plugin_section`，有插件时显示。单个插件数据异常只跳过该项，不能阻断其余插件显示。

- [ ] **Step 4: 实现当前 Tab 目标调用**

  增加 `_on_ui_plugin_clicked(card_id)`：沿父链查找拥有 `get_current_window` 的 `TabManagerWindow`，获取当前窗口后调用：

  ```python
  from app.core.ui_plugin_registry import UIPluginRegistry
  UIPluginRegistry.get_instance().toggle_floating_card(
      card_id,
      main_widget=current_window,
  )
  ```

  Tab 模式保证始终存在当前窗口；如果 Qt 关闭竞态导致目标为空，则安全返回并记录 warning，不创建伪窗口。

- [ ] **Step 5: 运行语法检查**

  Run: `python -m py_compile app/widgets/tab_panel.py`

  Expected: 命令成功退出，未生成需要纳入提交的源码垃圾文件。

---

### Task 2: 处理插件图标、主题和字号刷新

**Files:**
- Modify: `app/widgets/tab_panel.py:refresh_style` 及新增插件刷新辅助方法

- [ ] **Step 1: 复用插件管理器主题图标读取逻辑**

  通过 `PluginManager.get_instance().get_plugin(plugin_name)` 读取 `icon_config`，根据 `isDarkTheme()` 选择 dark/light 图标路径并创建 `QIcon`；插件没有图标时保留无图标按钮。图标尺寸使用现有 `scale_icon_size()`，不引入硬编码主题颜色。

- [ ] **Step 2: 在主题刷新中更新插件按钮样式**

  在现有 `refresh_style()` 中刷新插件标题、按钮的样式表和图标尺寸；刷新时不改变 `_plugin_infos`，避免仅换主题时重复访问注册表。插件新增/删除仍通过 `refresh_ui_plugins()` 处理。

- [ ] **Step 3: 验证主题刷新路径**

  Run: `python -m py_compile app/widgets/tab_panel.py`

  Expected: 语法检查通过；现有 Tab 样式刷新代码保持可调用。

---

### Task 3: 移除 Tab 模式共享 EdgeLauncher，接入 TabPanel 刷新

**Files:**
- Modify: `app/widgets/tab_manager_window.py:imports、__init__、_on_theme_changed、add_window、tab callbacks、hot reload helpers`

- [ ] **Step 1: 删除 TabManagerWindow 对共享 EdgeLauncher 的依赖**

  移除 `UIPluginEdgeLauncher` import、`_shared_edge_launcher` 字段、`_init_shared_launcher()` 调用及其初始化方法。不要删除 `app/widgets/ui_plugin_edge_launcher.py`。

- [ ] **Step 2: 移除共享入口的显示和隐藏调用**

  删除 `_update_shared_launcher()`、`_show_shared_launcher()`、`_hide_shared_launcher()` 调用和方法；保留 Tab 切换、窗口迁移、会话保存等无关逻辑。

- [ ] **Step 3: 保持 Tab 窗口内 EdgeLauncher 隐藏逻辑**

  `add_window()` 中继续调用 `_hide_edge_launcher(window)`，确保每个聊天窗口作为 Tab 嵌入时不显示独立窗口入口；该调用不影响多窗口模式，因为 `_hide_edge_launcher` 只在加入 Tab 时执行。

- [ ] **Step 4: 接入 TabPanel 插件刷新**

  在 TabManagerWindow 初始化完成 `_setup_ui()` 后，调用 `self._tab_panel.refresh_ui_plugins()`；在 UI 插件热重载、插件延迟加载完成等已有 Tab 刷新位置，改为调用 `_tab_panel.refresh_ui_plugins()`，并移除对共享 Launcher 的刷新代码。

  主题变化中保留 TabPanel 的 `refresh_style()`，不再调用共享 Launcher 的 `apply_theme()`。

- [ ] **Step 5: 运行语法检查**

  Run: `python -m py_compile app/widgets/tab_manager_window.py app/widgets/tab_panel.py`

  Expected: 两个文件语法检查通过。

---

### Task 4: 验证插件目标隔离与多窗口兼容

**Files:**
- Modify: `app/widgets/tab_panel.py`
- Modify: `app/widgets/tab_manager_window.py`

- [ ] **Step 1: 检查当前 Tab 目标切换**

  使用现有 Tab 切换流程确认按钮点击时动态调用 `get_current_window()`，不缓存创建时的窗口引用；切换 Tab 后再次点击同一插件，目标必须是新的活动窗口。

- [ ] **Step 2: 检查多窗口分支未被改动**

  确认 `app/main_widget.py` 中 `_ui_plugin_edge_launcher` 的创建、`refresh_plugins()`、resize 定位、主题刷新和 UI 热重载刷新调用仍存在。

- [ ] **Step 3: 运行项目检查**

  Run: `ruff check app/widgets/tab_panel.py app/widgets/tab_manager_window.py`

  Expected: 无新增 lint 错误；如发现仅由本任务引入的导入或格式问题，修复后重新执行。

  Run: `pytest tests/ -x`

  Expected: 测试通过；若环境缺少 PyQt 或已有无关变更导致失败，记录准确失败原因，不修改任务范围外文件。

- [ ] **Step 4: 检查差异范围**

  Run: `git diff -- app/widgets/tab_panel.py app/widgets/tab_manager_window.py`

  Expected: 差异只包含插件列表、当前 Tab 目标调用和 Tab 模式共享入口移除；不重置工作树中 `message_card.py`、`main.py`、`uv.lock` 等无关变更。

- [ ] **Step 5: 提交任务代码**

  ```bash
  git add app/widgets/tab_panel.py app/widgets/tab_manager_window.py
  git commit -m "feat: tab-ui-plugin-list - show plugins in tab panel"
  ```

  只提交任务指定的两个源码文件，不提交其他已有工作树变更。
