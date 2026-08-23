# -*- coding: utf-8 -*-
"""system_cards 模块 — 六张系统卡懒创建 + 项目选择卡

Phase F 瘦版：保留主程序 setup_ui 3106-3256 段的原代码不变。
本模块作为 override 入口——插件可完全替换系统卡的懒创建逻辑。

属性契约：
- _history_card _history_popup_card _share_card _share_card_content
- _history_questions_card _history_questions_card_content
- _tool_control_card _project_selector_card _project_selector_card_content
- _sub_agent_compact_widget
"""

from app.plugins.contracts.ui_module import UIModule


class SystemCardsModule(UIModule):
    """系统卡模块占位（系统默认实现保留在 main_widget.setup_ui 中）"""

    module_id = "system_cards"

    def build(self, host) -> None:
        # 系统默认实现仍在 main_widget.setup_ui 内。
        # 插件 override 时：完全替换本 build，host 属性应保持与系统实现同名。
        pass
