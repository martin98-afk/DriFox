# -*- coding: utf-8 -*-
"""复现：枚举列表选择一个值后，悬浮描述气泡位置/可见性不更新

用户操作：
1. 输入 /team --load= → 值选择模式弹出，气泡显示高亮项描述
2. 选择 default-team → 退出值选择模式，气泡应隐藏
3. 100ms 防抖后 _sync_detail_params → _auto_switch_to_value_selection
   因行尾空格不算"已离开"，--load= 仍匹配 → 重新进入值选择模式
   → 气泡重新显示，停留在旧几何位置（视觉上悬在聊天区中间）

预期：选择完成后气泡保持隐藏（或至少位置与卡片一致）。
"""

import sys

from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


def _make_card_with_load_param():
    """构造 detail 模式卡片：/team 命令，--load= 为 value 参数（动态枚举）"""
    from app.widgets.cards.floating.command_card import CommandCard
    from app.core.command_manager import CommandParameter

    parent = QWidget()
    parent.setLayout(QVBoxLayout())
    parent.resize(800, 600)
    card = CommandCard()
    parent.layout().addWidget(card)
    parent.show()
    app = QApplication.instance()
    for _ in range(5):
        app.processEvents()

    card._data_provider = {
        "template_options": [
            {"value": "default-team", "description": "经典四角色团队：统筹 + 构建 + 审查 + 计划"},
            {"value": "perf-team", "description": "性能团队"},
        ]
    }
    # 手动进入 detail 模式（跳过 CommandManager 依赖）
    card._detail_mode = True
    card._detail_cmd_name = "team"
    card._detail_selected_type = "command"
    card._detail_has_params = True
    card._value_selection_mode = False
    card._build_param_widgets(
        [CommandParameter(name="--load=", description="加载模板", param_type="value",
                          value_options=[{"value": "default-team", "description": "经典四角色团队"}])]
    )
    return parent, card


class TestBugRepro:
    def test_bubble_should_stay_hidden_after_value_selected(self):
        """选择枚举值后（含 100ms 防抖同步），气泡不得重新显示在旧位置"""
        _ensure_qapp()
        parent, card = _make_card_with_load_param()
        try:
            app = QApplication.instance()

            # Step 1: 输入 /team --load= → 进入值选择模式
            text1 = "/team --load="
            card.update_active_params(set(), full_text=text1, cursor_pos=len(text1))
            for _ in range(5):
                app.processEvents()
            assert card._value_selection_mode, "应进入值选择模式"
            lbl = card._desc_tooltip_label
            assert lbl is not None and lbl.isVisible(), "气泡应显示"

            # Step 2: 选择 default-team（键盘/点击同路径）
            card._selected_value_index = 0
            card.select_current()
            for _ in range(5):
                app.processEvents()
            assert not card._value_selection_mode, "选择后应退出值选择模式"
            assert not lbl.isVisible(), "选择后气泡应立即隐藏"

            # Step 3: 模拟 100ms 防抖到期后的同步（输入框文本已含完整值+空格）
            text2 = "/team --load=default-team "
            card.update_active_params(set(), full_text=text2, cursor_pos=len(text2))
            for _ in range(10):
                app.processEvents()

            # 修复前：_auto_switch_to_value_selection 因行尾空格不算离开，
            # 重新进入值选择模式 → 气泡重新显示（悬在旧位置）
            assert not card._value_selection_mode, (
                "防抖同步后不应重新进入值选择模式（用户已完成选择）"
            )
            assert not lbl.isVisible(), (
                "防抖同步后气泡不得重新显示（否则悬在旧几何位置）"
            )
        finally:
            parent.close()
            card.deleteLater()
            parent.deleteLater()
