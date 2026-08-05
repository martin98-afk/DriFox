# -*- coding: utf-8 -*-
"""窗口拖拽状态标志（原 tool_popup.ToolPopupDialog._any_window_dragging）

多窗口模式（ToolPopupDialog）已下线后，Tab 模式下的拖拽节流仍依赖
该全局标志：拖拽窗口期间各组件跳过耗时的布局重算与高度调整。

独立成模块避免循环导入（main_widget / tab_manager_window /
bottom_input_area / message_card / card_container / mcp_setting_card
均需读写该标志）。
"""

any_window_dragging: bool = False
