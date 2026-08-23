# -*- coding: utf-8 -*-
"""title_bar 模块 — 会话栏（项目/分支/标题/右侧按钮）

Phase F 瘦版：保留主程序 setup_ui 2877-3012 段的原代码不变。
本模块作为 override 入口——插件 register_ui_module(module_id="title_bar", priority>=100)
可完全替换会话栏的装配逻辑（接管 self._project_branch_container /
title_edit / _history_questions_btn / _share_btn / diff_btn 等属性的创建）。

属性契约（主程序 setup_ui 创建；模块 override 时应保持同名 setattr）：
- _project_branch_container _project_avatar _pb_separator _branch_widget
- _project_label title_edit _history_questions_btn _share_btn diff_btn
- _refresh_project_branch_style / _refresh_branch_widget_style 回调
"""

from app.plugins.contracts.ui_module import UIModule


class TitleBarModule(UIModule):
    """会话栏模块占位（系统默认实现保留在 main_widget.setup_ui 中）"""

    module_id = "title_bar"

    def build(self, host) -> None:
        # 系统默认实现仍在 main_widget.setup_ui 内（向后兼容，行为零变化）。
        # 插件 override 时：完全替换本 build，host 属性应保持与系统实现同名。
        # 此处留空：系统 setup_ui 继续按原代码执行；插件 override 接管后跳过系统代码。
        pass
