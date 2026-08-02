# -*- coding: utf-8 -*-
"""CommandCard 枚举值模式 tooltip 描述显示测试

背景：值选择列表（--model= / --join= / --load= / --plugin= 等枚举参数）
原来只显示枚举值本身、无任何描述。修复后：
- 枚举值条目支持 {"value", "description"} 结构（兼容纯字符串）
- ValueItemWidget 携带 description 字段
- 值选择模式下复用列表模式的顶部悬浮气泡显示当前选中枚举值的描述

本测试覆盖：
1. _split_value_entry 条目解析（str / dict 两种格式）
2. _filter_value_options 按 value 过滤（兼容 dict 条目）
3. _update_value_desc_tooltip 显示当前选中枚举值描述 / 空描述隐藏
4. _update_desc_tooltip 在值选择模式下走枚举值分支而非隐藏
"""

import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication


def _ensure_qapp():
    """确保 QApplication 可用"""
    return QApplication.instance() or QApplication(sys.argv)


def _make_card():
    """创建 CommandCard（不依赖真实窗口环境，测试逻辑层行为）"""
    from app.widgets.cards.floating.command_card import CommandCard

    card = CommandCard()
    # 直接构造值选择状态（跳过 detail 视图构建，聚焦 tooltip 逻辑）
    card._value_selection_mode = True
    card._value_selection_param = "--model="
    card._selected_value_index = -1
    card._last_selected_value_index = -1
    card._value_widgets = []
    return card


class TestSplitValueEntry:
    def test_str_entry(self):
        """纯字符串条目 → (value, "")"""
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import _split_value_entry

        assert _split_value_entry("gpt-4o") == ("gpt-4o", "")
        assert _split_value_entry("") == ("", "")

    def test_dict_entry(self):
        """dict 条目 → (value, description)"""
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import _split_value_entry

        assert _split_value_entry({"value": "gpt-4o", "description": "GPT-4o 官方模型"}) == (
            "gpt-4o",
            "GPT-4o 官方模型",
        )
        # 缺 description 字段 → 空串
        assert _split_value_entry({"value": "gpt-4o"}) == ("gpt-4o", "")


class TestFilterValueOptions:
    def test_str_options(self):
        """纯字符串选项列表按子串过滤（向后兼容）"""
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import CommandCard

        card = _make_card()
        opts = ["gpt-4o", "gpt-4o-mini", "claude-3"]
        assert card._filter_value_options(opts, "") == opts
        assert card._filter_value_options(opts, "gpt") == ["gpt-4o", "gpt-4o-mini"]
        # 大小写不敏感：小写化后按子串匹配，"GPT-4O-MINI" 同样命中
        assert card._filter_value_options(opts, "GPT-4O-MINI") == ["gpt-4o-mini"]
        assert card._filter_value_options(opts, "claude") == ["claude-3"]

    def test_dict_options(self):
        """dict 选项列表按 value 过滤"""
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import CommandCard

        card = _make_card()
        opts = [
            {"value": "gpt-4o", "description": "GPT-4o 官方模型"},
            {"value": "claude-3", "description": "Claude 3"},
        ]
        assert card._filter_value_options(opts, "") == opts
        assert card._filter_value_options(opts, "gpt") == [opts[0]]
        assert card._filter_value_options(opts, "claude") == [opts[1]]


class TestValueDescTooltip:
    def test_value_tooltip_shows_selected_desc(self):
        """值选择模式下 tooltip 显示当前选中枚举值的描述"""
        _ensure_qapp()
        from PyQt5.QtWidgets import QWidget, QVBoxLayout
        from app.widgets.cards.floating.command_card import CommandCard, ValueItemWidget

        # 有父窗口，使 tooltip 可创建（_ensure_desc_tooltip 需要 window()）
        parent = QWidget()
        parent.setLayout(QVBoxLayout())
        parent.resize(800, 600)
        card = CommandCard()
        parent.layout().addWidget(card)
        parent.show()
        app = QApplication.instance()
        for _ in range(5):
            app.processEvents()

        w0 = ValueItemWidget("gpt-4o", "GPT-4o 官方模型")
        w1 = ValueItemWidget("gpt-4o-mini", "轻量快速版")
        card._value_widgets = [w0, w1]
        card._value_selection_mode = True
        card._value_selection_param = "--model="

        # 选中第 0 项 → tooltip 文本 = w0 描述
        card._selected_value_index = 0
        card._last_selected_value_index = -1
        card._update_value_selection()
        assert card._desc_tooltip_label is not None, "tooltip 应被惰性创建"
        assert "GPT-4o 官方模型" in card._desc_tooltip_label.text()
        assert card._desc_tooltip_label.isVisible() is not False or card.width() <= 0

        # 切到第 1 项 → tooltip 文本更新为 w1 描述
        card._selected_value_index = 1
        card._update_value_selection()
        assert "轻量快速版" in card._desc_tooltip_label.text()

    def test_value_tooltip_hidden_when_no_desc(self):
        """选中项无描述 → tooltip 隐藏（不显示空白气泡）"""
        _ensure_qapp()
        from PyQt5.QtWidgets import QWidget, QVBoxLayout
        from app.widgets.cards.floating.command_card import CommandCard, ValueItemWidget

        parent = QWidget()
        parent.setLayout(QVBoxLayout())
        parent.resize(800, 600)
        card = CommandCard()
        parent.layout().addWidget(card)
        parent.show()
        app = QApplication.instance()
        for _ in range(5):
            app.processEvents()

        # 无描述的枚举值（纯字符串条目解析出的空描述）
        w = ValueItemWidget("legacy-model", "")
        card._value_widgets = [w]
        card._value_selection_mode = True
        card._selected_value_index = 0
        card._last_selected_value_index = -1
        card._update_value_selection()
        assert card._desc_tooltip_label is not None
        assert not card._desc_tooltip_label.isVisible(), "空描述应隐藏 tooltip"

    def test_update_desc_tooltip_value_branch(self):
        """_update_desc_tooltip 在值选择模式走枚举值描述分支而非隐藏"""
        _ensure_qapp()
        from PyQt5.QtWidgets import QWidget, QVBoxLayout
        from app.widgets.cards.floating.command_card import CommandCard, ValueItemWidget

        parent = QWidget()
        parent.setLayout(QVBoxLayout())
        parent.resize(800, 600)
        card = CommandCard()
        parent.layout().addWidget(card)
        parent.show()
        app = QApplication.instance()
        for _ in range(5):
            app.processEvents()

        w = ValueItemWidget("plugin-x", "插件 X：文件同步")
        card._value_widgets = [w]
        card._value_selection_mode = True
        card._selected_value_index = 0
        card._last_selected_value_index = -1
        # 直接调用统一入口
        card._update_desc_tooltip()
        assert card._desc_tooltip_label is not None
        assert "插件 X" in card._desc_tooltip_label.text()

    def test_non_value_detail_mode_hides_tooltip(self):
        """非值选择的 detail 模式（参数列表）仍隐藏 tooltip（回归保护）"""
        _ensure_qapp()
        from PyQt5.QtWidgets import QWidget, QVBoxLayout
        from app.widgets.cards.floating.command_card import CommandCard

        parent = QWidget()
        parent.setLayout(QVBoxLayout())
        parent.resize(800, 600)
        card = CommandCard()
        parent.layout().addWidget(card)
        parent.show()
        app = QApplication.instance()
        for _ in range(5):
            app.processEvents()

        # detail 参数列表模式（非值选择）→ 不显示 tooltip
        card._detail_mode = True
        card._value_selection_mode = False
        # 先让 tooltip 有文本（模拟列表模式残留），再切 detail
        card._desc_tooltip_label = None  # 强制惰性重建
        card._update_desc_tooltip()
        assert card._desc_tooltip_label is not None
        assert not card._desc_tooltip_label.isVisible(), "参数列表 detail 模式不应显示 tooltip"
