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

    def test_exit_value_selection_hides_tooltip(self):
        """退出值选择模式后气泡隐藏（选中值描述不残留）"""
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

        # 进入值选择模式并选中带描述的值 → 气泡显示描述
        w = ValueItemWidget("plugin-x", "插件 X：文件同步")
        card._value_widgets = [w]
        card._value_selection_mode = True
        card._value_selection_param = "--plugin="
        card._selected_value_index = 0
        card._last_selected_value_index = -1
        card._update_value_selection()
        assert card._desc_tooltip_label is not None
        assert card._desc_tooltip_label.isVisible() or card.width() <= 0, "值选择模式气泡应显示"
        if card.width() > 0:
            assert "插件 X" in card._desc_tooltip_label.text()

        # 退出值选择模式 → 气泡隐藏（不残留刚选中值的描述）
        card._exit_value_selection()
        assert not card._value_selection_mode
        assert not card._desc_tooltip_label.isVisible(), "退出值选择后气泡应隐藏"

    def test_value_tooltip_resolves_quote_entity(self):
        """描述含引号时 tooltip 强制 RichText，&quot; 实体解析为真实引号

        回归：html.escape 后 setText，QLabel AutoText 只认字面 < 与 "& "，
        不认 &quot; 实体 → 判定 PlainText → &quot; 字面显示。
        修复：setText 前显式 setTextFormat(Qt.RichText)。
        """
        _ensure_qapp()
        import html as html_mod

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

        w = ValueItemWidget("model-x", '支持 "引号" 与 <尖括号>')
        card._value_widgets = [w]
        card._value_selection_mode = True
        card._value_selection_param = "--model="
        card._selected_value_index = 0
        card._last_selected_value_index = -1
        card._update_value_selection()

        lbl = card._desc_tooltip_label
        assert lbl is not None
        # 强制 RichText：实体在渲染时被解析（此前 PlainText 判定会字面显示 &quot;）
        assert lbl.textFormat() == Qt.RichText, f"tooltip 应强制 RichText，实际 {lbl.textFormat()}"
        assert "&quot;" in lbl.text(), "存储形式应为实体"
        assert '"引号"' in html_mod.unescape(lbl.text()), "unescape 后应还原为真实引号"
        assert "<尖括号>" in html_mod.unescape(lbl.text()), "unescape 后应还原为尖括号原文"

    def test_list_tooltip_resolves_quote_entity(self):
        """列表模式悬浮气泡描述含引号时同样解析实体（_update_desc_tooltip 主链路）"""
        _ensure_qapp()
        import html as html_mod

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

        card._filtered_items = [{"name": "skill-x", "type": "skill", "description": '含 "引号" 的描述'}]
        card._selected_index = 0
        card._detail_mode = False
        card._value_selection_mode = False
        card._current_text_query = ""
        card._update_desc_tooltip()

        lbl = card._desc_tooltip_label
        assert lbl is not None
        assert lbl.textFormat() == Qt.RichText
        assert '"引号"' in html_mod.unescape(lbl.text())

    def test_detail_desc_escapes_html(self):
        """detail 模式命令描述含 < > 时按纯文本安全显示（不解析为 HTML 标签）"""
        _ensure_qapp()
        import html as html_mod

        from PyQt5.QtWidgets import QWidget, QVBoxLayout
        from app.core.command_manager import CommandManager, CommandType
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

        cmd_mgr = CommandManager.get_instance()
        test_name = "__test_detail_desc__"
        cmd_mgr.register(test_name, CommandType.FUNCTION, description='含 <b> 与 "引号" 的说明')
        try:
            card.show_command_detail(test_name, selected_type="command")
            # escape + 强制 RichText：text() 存实体形式，渲染时解析回原文
            assert card._detail_desc_label.textFormat() == Qt.RichText
            assert "&lt;b&gt;" in card._detail_desc_label.text(), "尖括号应被转义存储"
            assert "<b>" in html_mod.unescape(card._detail_desc_label.text()), "unescape 后还原为原文"
        finally:
            cmd_mgr.unregister(test_name)


class TestCursorPastParamValue:
    """_cursor_past_param_value 光标离开判定回归测试（T1 修复）。

    背景：手打路径（textChanged → _sync_detail_params → _auto_switch_to_value_selection）
    通过 _cursor_past_param_value 判断「是否已离开该参数」，若把行尾空格误判为
    「已离开」，手打完整值后枚举列表永不弹出（与 Tab 路径不一致）。

    语义（修复后）：
    - 行尾空格（空格后无实质内容，刚打完值）→ 不算离开 → 应触发值选择
    - 空格后已有下一参数且光标越过该空格 → 算离开 → 跳过
    - 光标 < 0（无光标信息）→ 不算离开
    - 无空格 → 不算离开
    """

    @staticmethod
    def _token_end(text: str, param: str = "--model=") -> int:
        """定位参数名+等号结束位置（与 _auto_switch_to_value_selection 的正则语义一致）"""
        idx = text.find(param)
        assert idx >= 0, f"文本中未找到 {param}: {text!r}"
        return idx + len(param)

    def _assert_leave(self, text: str, cursor_pos: int, expected_leave: bool):
        """断言光标离开判定结果（True=已离开/应跳过，False=未离开/应触发）"""
        from app.widgets.cards.floating.command_card import CommandCard

        card = CommandCard()
        token_end = self._token_end(text)
        result = card._cursor_past_param_value(text, token_end, cursor_pos)
        assert result is expected_leave, (
            f"text={text!r} cursor={cursor_pos} token_end={token_end} → got {result}, expected {expected_leave}"
        )

    # ── 触发路径：应返回 False（未离开 → 触发值选择）──

    def test_full_value_no_trailing_space(self):
        """手打完整值无尾空格 → 未离开（触发）"""
        self._assert_leave("/subagent --model=gpt", 21, False)

    def test_full_value_with_trailing_space(self):
        """手打完整值+行尾空格 → 未离开（触发）——T1 核心回归"""
        self._assert_leave("/subagent --model=gpt ", 22, False)

    def test_value_prefix_no_trailing_space(self):
        """手打值前缀无尾空格 → 未离开（触发）"""
        self._assert_leave("/subagent --model=g", 20, False)

    def test_value_prefix_with_trailing_space(self):
        """手打值前缀+行尾空格 → 未离开（触发）"""
        self._assert_leave("/subagent --model=g ", 21, False)

    def test_only_param_name_with_equals(self):
        """仅参数名+=号 → 未离开（触发）"""
        self._assert_leave("/subagent --model=", 19, False)

    def test_cursor_mid_value_before_trailing_space(self):
        """值中间光标（尾空格前）→ 未离开（触发）"""
        self._assert_leave("/subagent --model=gpt ", 20, False)

    def test_cursor_on_trailing_space(self):
        """光标停在行尾空格上 → 未离开（触发）"""
        self._assert_leave("/subagent --model=gpt ", 21, False)

    # ── 退出/跳过路径：应返回 True（已离开 → 跳过/退出）──

    def test_next_param_cursor_past(self):
        """值+下一参数且光标在其后 → 已离开（跳过，不弹 model 枚举）"""
        self._assert_leave("/subagent --model=gpt --quick", 26, True)

    def test_next_param_typing(self):
        """值+下一参数输入中（光标在下一参数内）→ 已离开（跳过）"""
        self._assert_leave("/subagent --model=gpt --q", 25, True)

    # ── 边界 ──

    def test_cursor_negative(self):
        """cursor_pos=-1（无光标信息）→ 未离开（触发）"""
        self._assert_leave("/subagent --model=gpt ", -1, False)

    def test_no_space_after_value(self):
        """值后无空格 → 未离开（触发）"""
        self._assert_leave("/subagent --model=gpt", 30, False)

    def test_multiple_trailing_spaces(self):
        """行尾多空格（光标在两个空格之间）→ 未离开（触发）

        边界：多个行尾空格时 text.find 只命中第一个空格，若仅凭 cursor > 第一个
        空格即判定"离开"会误判；修复后的 strip 语义对任意数量的行尾空白都视为
        "未离开"，与光标落在空格序列中任何位置一致。
        """
        self._assert_leave("/subagent --model=gpt  ", 22, False)

    def test_tab_trailing(self):
        """行尾 tab 尾缀 → 未离开（触发）

        全空白尾缀（tab）与空格等价，不应误判为"已离开"。
        """
        self._assert_leave("/subagent --model=gpt\t", 22, False)
