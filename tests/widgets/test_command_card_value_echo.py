# -*- coding: utf-8 -*-
"""枚举选择后防抖回声重入回归测试

背景（bug）：值选择列表（--load= / --model= 等）选中一项后（Tab/Enter/点击），
插入值触发 textChanged → 100ms 防抖 → _sync_detail_params →
_auto_switch_to_value_selection。因行尾空格不算「已离开」（T1 语义），
同一参数再次命中 → 重新弹回值选择模式，枚举描述气泡随之重新显示；
且气泡定位发生在 _adjust_detail_height 改变卡片几何之前，
视觉上悬浮窗悬在旧位置（聊天区中间）。

修复：
- _exit_value_selection(mark_selected=True) 仅在程序化选择路径打回声标记
- _auto_switch_to_value_selection 命中同参数的首次防抖同步视为回声，直接跳过
- _switch_to_value_selection 先 _adjust_detail_height 再 _update_value_selection，
  气泡定位基于更新后的卡片几何
"""

import sys

from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


def _make_card():
    """构造 detail 模式卡片：--load= / --join= 两个 value 参数（动态枚举）"""
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
        ],
        "agent_options": [{"value": "leader", "description": "统筹"}],
    }
    card._detail_mode = True
    card._detail_cmd_name = "team"
    card._detail_selected_type = "command"
    card._detail_has_params = True
    card._value_selection_mode = False
    card._build_param_widgets([
        CommandParameter(name="--load=", description="加载模板", param_type="value",
                         value_options=[{"value": "default-team", "description": "经典四角色团队"}]),
        CommandParameter(name="--join=", description="加入角色", param_type="value",
                         value_options=[{"value": "leader", "description": "统筹"}]),
    ])
    return parent, card


class TestEchoSuppression:
    def test_no_reenter_after_value_selected(self):
        """选择完成后，防抖同步不得重新弹回值选择模式（气泡保持隐藏）"""
        _ensure_qapp()
        parent, card = _make_card()
        try:
            app = QApplication.instance()

            def sync(text):
                card.update_active_params(set(), full_text=text, cursor_pos=len(text))
                for _ in range(5):
                    app.processEvents()

            sync("/team --load=")
            assert card._value_selection_mode, "手打 --load= 应弹出枚举列表"
            lbl = card._desc_tooltip_label
            assert lbl is not None and lbl.isVisible(), "气泡应显示选中项描述"

            card._selected_value_index = 0
            card.select_current()
            for _ in range(5):
                app.processEvents()
            assert not card._value_selection_mode
            assert not lbl.isVisible()

            sync("/team --load=default-team ")  # 100ms 防抖回声
            assert not card._value_selection_mode, "回声同步不得重新弹回枚举列表"
            assert not lbl.isVisible(), "回声同步后气泡不得重新显示"
        finally:
            parent.close()
            card.deleteLater()
            parent.deleteLater()

    def test_retype_after_selection_reopens_list(self):
        """选择后删字重新编辑 → 枚举列表应正常重开（回声标记只抑制一次且不误伤）"""
        _ensure_qapp()
        parent, card = _make_card()
        try:
            app = QApplication.instance()

            def sync(text):
                card.update_active_params(set(), full_text=text, cursor_pos=len(text))
                for _ in range(5):
                    app.processEvents()

            sync("/team --load=")
            card._selected_value_index = 0
            card.select_current()
            for _ in range(5):
                app.processEvents()
            sync("/team --load=default-team ")  # 回声
            assert not card._value_selection_mode

            sync("/team --load=def")  # 用户删值重新输入
            assert card._value_selection_mode, "用户重新编辑应重开枚举列表"
            assert len(card._value_widgets) == 1
        finally:
            parent.close()
            card.deleteLater()
            parent.deleteLater()

    def test_switch_param_clears_echo(self):
        """选完 --load= 后切到 --join= → 回声标记不影响新参数列表弹出"""
        _ensure_qapp()
        parent, card = _make_card()
        try:
            app = QApplication.instance()

            def sync(text):
                card.update_active_params(set(), full_text=text, cursor_pos=len(text))
                for _ in range(5):
                    app.processEvents()

            sync("/team --load=")
            card._selected_value_index = 0
            card.select_current()
            for _ in range(5):
                app.processEvents()

            sync("/team --load=default-team --join=")  # 光标已越过 --load 值
            assert card._value_selection_mode, "应切换到 --join= 的枚举列表"
            assert card._value_selection_param == "--join="
            assert [w.value for w in card._value_widgets] == ["leader"]
            assert card._value_just_selected_param == "", "放行后标记应清除"
        finally:
            parent.close()
            card.deleteLater()
            parent.deleteLater()

    def test_context_exit_does_not_set_echo(self):
        """参数被删导致的退出（非选择路径）不得打回声标记

        否则用户删除 --load= 值重新输入时，列表会被误抑制不弹出。
        """
        _ensure_qapp()
        parent, card = _make_card()
        try:
            app = QApplication.instance()

            def sync(text, active):
                card.update_active_params(active, full_text=text, cursor_pos=len(text))
                for _ in range(5):
                    app.processEvents()

            sync("/team --load=", {"--load="})
            assert card._value_selection_mode

            # 用户删掉整个参数（active 集合不再含 --load=）→ 情境退出
            sync("/team ", set())
            assert not card._value_selection_mode
            assert card._value_just_selected_param == "", "情境退出不得打回声标记"

            # 重新输入 → 应正常弹出
            sync("/team --load=", {"--load="})
            assert card._value_selection_mode, "重新输入参数应重开枚举列表"
        finally:
            parent.close()
            card.deleteLater()
            parent.deleteLater()
