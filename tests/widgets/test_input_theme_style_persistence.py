# -*- coding: utf-8 -*-
"""输入框主题切换样式存活回归测试。

背景：SendableTextEdit 继承 qfluentwidgets 的 TextEdit，构造时被注册进
qfw styleSheetManager（LINE_EDIT 源）。历史上 DriFox 用裸 setStyleSheet
覆盖为「透明融入卡片」样式，但任何一次 qfw setTheme 都会 updateStyleSheet
遍历重设所有注册 widget 的 Fluent QSS，把 DriFox 样式顶掉；一旦 DriFox
刷新链因幂等跳过（_last_color_theme_id 未变 → is_color=False → 5a 块跳过）
或异常中断没盖回，输入框就停留在 Fluent 白底描边样式（楷体 placeholder +
青色下划线）。

修复：_apply_input_style / _apply_agent_combo_style 改走 qfw 官方
setCustomStyleSheet 通道，自定义 QSS 并入 styleSheetManager 组合源，
主题切换重设时自动携带，不再被顶掉。
（同步自 pyside6 分支 74a0d290）
"""

import pytest
from qfluentwidgets import Theme, setTheme

from app.widgets.bottom_input_area import SendableTextEdit

_MARK_INPUT = "background: transparent"  # DriFox 输入框样式特征（shorthand）
_MARK_COMBO = "border-radius: 10px"  # DriFox 下拉框特征圆角（qfw 默认 5px）


def _assert_styles_alive(editor: SendableTextEdit, note: str):
    assert _MARK_INPUT in editor.styleSheet(), f"[{note}] 输入框自定义样式被 qfw 顶掉"
    assert _MARK_COMBO in editor._agent_combo.styleSheet(), f"[{note}] 下拉框自定义样式被 qfw 顶掉"


def test_custom_style_survives_theme_roundtrip(qapp):
    """主题 LIGHT/DARK 往返多次，自定义样式必须始终存活。"""
    setTheme(Theme.LIGHT)
    editor = SendableTextEdit()
    _assert_styles_alive(editor, "构造后")

    for theme in (Theme.DARK, Theme.LIGHT, Theme.DARK, Theme.LIGHT):
        setTheme(theme)
        _assert_styles_alive(editor, f"setTheme({theme})")


def test_refresh_style_keeps_custom_qss(qapp):
    """DriFox 刷新链（refresh_style）后样式仍存活且为最新内容。"""
    setTheme(Theme.DARK)
    editor = SendableTextEdit()
    editor.refresh_style()
    _assert_styles_alive(editor, "refresh_style 后")
    # setCustomStyleSheet 通道下 styleSheet 为 qfw qss + custom 组合，
    # custom 必须位于其后（同特异性靠后覆盖 qfw 的白底/描边规则）
    assert editor.styleSheet().rstrip().endswith("}")  # 结构完整性


def test_focus_hover_state_not_leaked_from_fluent_qss(qapp):
    """qfw :focus/:hover 规则（特异性 0,1,1）不得渗入背景/边框。

    qfw 亮色 qss 的 TextEdit:focus 声明 background-color: white 与青色
    border-bottom；custom 基础规则特异性(0,0,1)压不住，必须显式覆盖。
    """
    setTheme(Theme.LIGHT)
    editor = SendableTextEdit()
    editor.refresh_style()
    qss = editor.styleSheet()
    # 组合样式表中必须存在对 focus/hover 态的显式透明背景覆盖
    assert "QTextEdit:hover" in qss and "QTextEdit:focus" in qss
    assert qss.count("background: transparent") >= 3  # 基础 + hover/focus + disabled
