# -*- coding: utf-8 -*-
"""bottom_toolbar 模块 — 工具栏条/模型按钮/capsule/光晕

Phase F 瘦版：保留主程序 setup_ui 3413-3709 段的原代码不变。
本模块作为 override 入口——插件可完全替换工具栏装配逻辑。

属性契约：
- _bottom_toolbar_strip _model_btn_container current_model_btn
- settings_btn effort_btn _tool_toggle_btn _toolbar_capsule
- memory_btn history_btn new_session_btn
- _input_glow_underlay
"""

from app.plugins.contracts.ui_module import UIModule


class BottomToolbarModule(UIModule):
    """工具栏模块占位（系统默认实现保留在 main_widget.setup_ui 中）"""

    module_id = "bottom_toolbar"

    def build(self, host) -> None:
        # 系统默认实现仍在 main_widget.setup_ui 内。
        pass
