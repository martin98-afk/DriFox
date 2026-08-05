# -*- coding: utf-8 -*-
"""废弃占位模块（原 ToolPopupDialog 多窗口模式）

本模块原定义 ToolPopupDialog（旧多窗口模式）及其专用组件
（OpacitySlider / LockButtonWidget / AdaptiveStackedWidget / ResizeEdge）。

多窗口模式已下线，M3 删除唯一实例化点后本模块内容全部废弃：

- ToolWindow / ToolWindowTitleBar → 迁移至 app/main_widget.py
- _WINDOWS_MSG → 迁移至 app/widgets/tab_manager_window.py
- ToolPopupDialog._any_window_dragging → 迁移至 app/utils/window_drag_state.py
  （模块级 any_window_dragging）

保留本文件仅为避免模块路径断裂（无任何代码再引用本模块，
`from app.tool_popup import ...` 已全部清除）。
"""
