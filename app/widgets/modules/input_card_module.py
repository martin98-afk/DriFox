# -*- coding: utf-8 -*-
"""input_card 模块 — 输入卡/附件区/命令三卡

Phase F 瘦版：保留主程序 setup_ui 3262-3410 段的原代码不变。
本模块作为 override 入口——插件可完全替换输入区装配逻辑。

属性契约：
- _bottom_input_container _input_card _input_card_wrapper
- _attach_container _attach_layout input_area
- _command_card _file_mention_card _undo_delete_card
- _attachments _history_working_attachments
"""

from app.plugins.contracts.ui_module import UIModule


class InputCardModule(UIModule):
    """输入卡模块占位（系统默认实现保留在 main_widget.setup_ui 中）"""

    module_id = "input_card"

    def build(self, host) -> None:
        # 系统默认实现仍在 main_widget.setup_ui 内。
        pass
