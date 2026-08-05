# -*- coding: utf-8 -*-
"""枚举参数手动输入触发回归测试。"""

from unittest.mock import patch

from app.core.command_manager import CommandParameter


def test_manual_value_parameter_detection_is_not_blocked_by_hidden_params(qapp):
    """参数列表被过滤为空时，手输完整 value 参数仍应触发枚举列表。"""
    from app.widgets.cards.floating.command_card import CommandCard

    card = CommandCard()
    card._detail_mode = True
    card._detail_has_params = True
    card._detail_cmd_name = "demo"
    card._param_widgets = []
    # 没有可见项时 update_active_params 会尝试从命令管理器重建，
    # 这里直接提供一个隐藏的 value 参数，模拟手输过程中的列表状态。
    class _ParamWidget:
        param_type = "value"
        param_name = "--model="
        _param = CommandParameter("--model=", "", param_type="value")

        def set_active(self, active):
            pass

        def isVisible(self):
            return False

        def setVisible(self, visible):
            pass

        def set_selected(self, selected):
            pass

    card._param_widgets = [_ParamWidget()]

    with patch.object(card, "_auto_switch_to_value_selection") as detect:
        # 用空参数列表模拟手动输入导致的参数项暂时不可见状态。
        card.update_active_params(set(), full_text="/demo --model=", cursor_pos=13)

    detect.assert_called_once_with("/demo --model=", 13)
