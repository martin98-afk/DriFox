# Tab 管理器设计

> 2026-07-25 | 状态: 待实现

## 概述

DriFox 已支持多窗口自由创建/关闭，但缺乏有效的窗口管理手段。本设计新增一个**可开关的 Tab 管理器**，与现有独立浮动窗口模式平行共存。启用时，所有窗口被吸入一个宿主窗口，左侧 Tab 面板 + 右侧嵌入窗口，切换 Tab 即切换窗口。

## 设计目标

- 提供一个类似浏览器 Tab 页的窗口管理体验
- 与现有独立窗口模式通过配置开关切换，不破坏现有行为
- 切换过程窗口状态（会话、滚动、输入内容）完全保留
- 利用 Qt `QWidget::setParent()` 实现内存共享迁移（同一份 `OpenAIChatToolWindow` 实例在两种模式下复用，无需重建）
- 视觉风格与 DriFox 现有主题体系一致

## 架构设计

### 顶层组件

```
┌─────────────────────────────────────────────────┐
│              TabManagerWindow                     │
│  ┌──────────────┐  ┌───────────────────────────┐ │
│  │  TabPanel     │  │  ContentArea              │ │
│  │  (左侧)       │  │  QStackedWidget           │ │
│  │               │  │                           │ │
│  │  📄 项目A    │  │  [当前选中的               │ │
│  │  🤖 代码审查  │  │   OpenAIChatToolWindow]   │ │
│  │  💬 闲聊     │  │                           │ │
│  │  🐛 调试     │  │                           │ │
│  │  🔍 研究     │  │                           │ │
│  │               │  │                           │ │
│  │  [+ 新建]    │  │                           │ │
│  └──────────────┘  └───────────────────────────┘ │
└─────────────────────────────────────────────────┘
```

### 新增文件

**`app/widgets/tab_manager_window.py`** — Tab 管理器宿主窗口

```
class TabManagerWindow(QWidget):
    """Tab 管理器宿主窗口。左侧 Tab 面板，右侧 QStackedWidget 嵌入窗口。"""

    # 核心组件
    - _tab_panel: TabPanel          # 左侧 Tab 列表
    - _content_area: QStackedWidget # 右侧内容区
    - _windows: List[OpenAIChatToolWindow]  # 所有嵌入的窗口引用
    - _cached_dialogs: Dict[id, ToolPopupDialog]  # 独立模式下缓存的对话框

    # 状态
    - _is_transitioning: bool       # 模式切换中保护锁
    - _active_tab_index: int        # 当前激活的 Tab 索引

    # 信号
    - tab_count_changed = pyqtSignal(int)
    - active_tab_changed = pyqtSignal(int)

**Tab 面板 ↔ 内容区索引同步机制：**

TabPanel 拖拽排序后发射 `tabs_reordered(list)`，TabManagerWindow 的槽函数将 QStackedWidget 的页面顺序与之对齐：

```python
def _on_tabs_reordered(self, new_order: list):
    # new_order 包含按新顺序排列的 widget 引用
    for i, w in enumerate(new_order):
        # 将要排在第 i 位的 widget 插入到第 i 位
        self._content_area.insertWidget(i, w)
```

**空状态页（EmptyStateWidget）：**

TabManagerWindow 内置一个 `QWidget` 作为空状态页，在最后一个 Tab 关闭时显示：
- 居中的大号 + 图标
- 「新建标签页」文字按钮
- 点击按钮 → 调用 `_duplicate_window(branch=False)` 创建新窗口
- 空状态页本身也是 QStackedWidget 的一页（索引 0），其他窗口从索引 1 开始
```

**`app/widgets/tab_panel.py`** — 左侧 Tab 面板

```
class TabPanel(QWidget):
    """自定义 Tab 列表面板，支持拖拽排序、右键菜单。"""

    - tab_selected = pyqtSignal(int)      # 选中 Tab
    - tab_close_requested = pyqtSignal(int)  # 关闭 Tab
    - new_tab_requested = pyqtSignal()    # 新建 Tab
    - tabs_reordered = pyqtSignal(list)   # 拖拽排序后新顺序

    # 每个 Tab 项：
    - icon: QLabel        # Agent 图标
    - title: QLabel       # 会话标题
    - close_btn: QPushButton  # 关闭按钮 (x)
```

### 改动文件

**`app/widgets/settings/llm_settings_card.py`** — LLM 设置卡片（通用 Tab）

- 在「通用设置」Tab 下新增「多窗口管理」分组
- 添加 `启用 Tab 管理器` 开关（QSwitchSettingCard）
- 关联配置键 `enable_tab_manager`
- 开关切换时触发 `TabManagerWindow.toggle_mode()`

**`app/tray_manager.py`** — 托盘管理器

- 新增 `_tab_manager_window` 属性，Tab 模式开启时指向 TabManagerWindow
- Tab 模式开启时：Tray 菜单不再列出各窗口，改为「Tab 管理器」单一项
- Tab 模式关闭后：恢复正常窗口列表
- `_toggle_all_windows` 检测 `_tab_manager_window` 引用：有则切换 TabManagerWindow 而非独立窗口

  ```python
  def _toggle_all_windows(self):
      if self._tab_manager_window is not None:
          if self._tab_manager_window.isVisible():
              self._tab_manager_window.hide()
          else:
              self._tab_manager_window.show()
          return
      # 原有独立窗口逻辑...
  ```

**`app/main_widget.py`** — 主窗口逻辑

- 新增 `_tab_mode_active` 实例属性（独立于 `_is_duplicate_window`），从 Settings 读 `enable_tab_manager`
- `_duplicate_window` 开头增加 Tab 模式分支判断：

  ```python
  def _duplicate_window(self, branch=False):
      if Settings.get_instance().enable_tab_manager.value:
          # Tab 模式：直接创建并加入 TabManagerWindow
          new_instance = OpenAIChatToolWindow(self.homepage, self)
          TabManagerWindow.get_instance().add_window(new_instance)
          return
      # 原有逻辑：创建 ToolPopupDialog...
  ```

- Tab 模式下新建窗口不经过 `ToolPopupDialog`，`OpenAIChatToolWindow` 的标题栏在嵌入时自动隐藏

## 核心交互

### Tab 面板

| 操作 | 行为 |
|---|---|
| 单击 Tab | 切换到对应窗口 |
| 拖拽 Tab | 重新排序（QStackedWidget 页面同步重排） |
| 右键 Tab | 弹出菜单：关闭 / 复制窗口 / 分支窗口 / 重命名 |
| 滚轮 | Tab 列表滚动（列表超长时） |
| 点击 [+ 新建] | 创建新窗口，自动切换到新 Tab |
| 最后一个 Tab 关闭 | 显示空状态引导页 |

### 键盘快捷键（预留，暂不实现）

| 快捷键 | 行为 |
|---|---|
| `Ctrl+Tab` | 下一个 Tab |
| `Ctrl+Shift+Tab` | 上一个 Tab |
| `Ctrl+W` | 关闭当前 Tab |
| `Ctrl+T` | 新建 Tab |
| `Ctrl+1~9` | 跳转到第 N 个 Tab |

### 窗口生命周期

**新建窗口（Tab 模式下）：**

1. 触发：`[+ 新建]` / 右键「复制窗口」/ `/new-window`
2. 直接创建 `OpenAIChatToolWindow`（不经过 `ToolPopupDialog`）
3. 添加到 `QStackedWidget` → 新页
4. Tab 面板末尾新增一项
5. 自动切换到新 Tab

**关闭窗口（Tab 模式下）：**

1. 触发：右键 Tab → 关闭
2. 调用 `OpenAIChatToolWindow` 现有关闭逻辑（自动保存会话）
3. 从 `QStackedWidget` 移除
4. 从 Tab 面板移除
5. 若关闭的是当前 Tab → 切换到相邻 Tab（右优先）
6. 最后一个 Tab 关闭 → 显示空状态页

## 模式切换逻辑

### 设置开关

配置键：`enable_tab_manager`（bool，默认 false）

位于「设置 → 多窗口」分组。

### 启用 Tab 模式（false → true）

```
1. 检查 TabManagerWindow 实例
   → 无则创建 TabManagerWindow
2. 遍历 TrayManager._windows（所有 ToolPopupDialog）
3. 对每个 dialog:
   a. 获取 dialog.tool_instance (OpenAIChatToolWindow)
   b. 从 dialog layout 中 removeWidget(tool_instance)
   c. tool_instance.setParent(content_area)
   d. content_area.addWidget(tool_instance)
   e. 从 _popup_refs 中清除已迁移 popup 的引用（防内存泄漏）
   f. 缓存 dialog 引用供恢复
   g. 调用 TrayManager.unregister_window(dialog)（清理 Tray 列表）
4. TrayManager._tab_manager_window = self
5. 更新 Tray 菜单 → 「Tab 管理器」单一项
6. 显示 TabManagerWindow，选中第一个 Tab
```

### 关闭 Tab 模式（true → false）

```
1. 遍历 content_area 中所有 window
2. 对每个 window:
   a. 创建/恢复 ToolPopupDialog（复用缓存的）
   b. window.setParent(dialog)
   c. 添加到 dialog layout
   d. 显示 dialog，恢复位置
   e. TrayManager.register_window(dialog)（重新注册）
3. 清空 content_area
4. TrayManager._tab_manager_window = None
5. 隐藏 TabManagerWindow（不销毁，下次切换时复用缓存）
6. 恢复 Tray 菜单正常窗口列表
```

### 启动时自动进入

```
1. 读取 enable_tab_manager 配置
2. 若为 true:
   a. 不创建独立 ToolPopupDialog
   b. 直接创建 TabManagerWindow + 首个窗口
3. 若为 false:
   a. 保持现有行为（独立窗口）
```

## 配置项

| 键 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enable_tab_manager` | bool | false | 启用 Tab 管理器 |
| `tab_panel_width` | int | 200 | 左侧 Tab 面板宽度（像素） |
| `tab_panel_collapsed` | bool | false | Tab 面板是否折叠（仅图标） |
| `tab_manager_geometry` | str | "" | TabManagerWindow 位置大小（JSON） |

## 边界情况

| 场景 | 处理 |
|---|---|
| 最后一个 Tab 关闭 | 显示 EmptyStateWidget（QStackedWidget 第 0 页）：大号 + 图标 + 「新建标签页」按钮 |
| 窗口有未保存内容 | 复用 `OpenAIChatToolWindow` 现有关闭保存逻辑 |
| 模式切换中用户连续操作 | `_is_transitioning` 保护锁 + 半透明遮罩 |
| 迁移中某个窗口异常 | try/except 逐个保护，跳过异常窗口，汇总提示 |
| Tab 模式开启时 Alt+Z | TrayManager 检测 `_tab_manager_window` 引用，切换整窗可见性 |
| 多屏幕场景 | TabManagerWindow 创建在用户主屏幕（与现有 ToolPopupDialog 行为一致），首次启用 Tab 模式时使用当前活动窗口所在屏幕 |
| macOS Dock 点击恢复 | Tab 模式下 Dock 点击恢复 TabManagerWindow（TrayManager 安装 macOS 事件过滤器指向 TabManagerWindow） |
| Tab 模式下嵌入窗口的标题栏 | `OpenAIChatToolWindow.get_title_bar().hide()` — 标题栏操作由 Tab 面板提供，原标题栏在嵌入时隐藏 |
| Tab 最小化行为 | 点击最小化 → 整个 TabManagerWindow 最小化到任务栏；各窗口不保留独立任务栏按钮 |
| Tab 面板图标 | 动态显示当前会话的 Agent 图标（源自 `OpenAIChatToolWindow.icon` 类属性 + 当前 active agent），无 Agent 时使用默认应用图标 |

## 测试要点

- 启用 Tab 模式 → 所有独立窗口正确迁入 Tab 面板
- 关闭 Tab 模式 → 所有窗口正确恢复为独立窗口
- 切换过程中窗口状态（会话、滚动位置）完全保留
- Tab 模式下新建窗口 → 正确创建并显示为 Tab
- 关闭 Tab → 窗口正确保存并从列表中移除
- 最后一个 Tab 关闭 → 显示空状态页
- 快速反复切换模式 → 保护锁正常工作（无竞态）
- Tab 拖拽排序 → QStackedWidget 页面顺序同步更新
- 深色/浅色主题切换 → Tab 面板配色正确适配
- TrayManager 在 Tab 模式下 `_windows` 列表为空，`_tab_manager_window` 正确指向 TabManagerWindow
- `_popup_refs` 在模式切换后无残留引用（无内存泄漏）

## 未涵盖（后续迭代）

- 键盘快捷键
- 窗口分组/标签
- 布局保存与恢复（多个命名布局）
- Tab 缩略图预览
- 窗口间拖拽消息
