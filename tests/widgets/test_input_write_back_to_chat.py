# -*- coding: utf-8 -*-
"""回归：input.write 降级解析真实输入框 + 写入后强制回对话视图

场景（2026-10-25 magpie「让 AI 帮我装」实测）：Tab 模式 full 卡覆盖对话区时
caps_for 解析的 host 是 TabManagerWindow——其上无 input_area（输入区在活跃
对话窗口），旧实现直接 raise「输入框不可用」；即便写入成功，用户停留在覆盖
页也看不到写入结果。
"""
import sys

from PySide6.QtWidgets import QApplication, QTextEdit, QWidget


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


class _Win(QWidget):
    """对话窗口 stub：带输入区 + 回对话调用记录"""

    def __init__(self):
        super().__init__()
        self.input_area = QTextEdit()
        self.conv_calls = []

    def _show_conversation_view(self):
        self.conv_calls.append(True)


class _TabHost(QWidget):
    """TabManagerWindow stub：无 input_area，有 _windows/_tab_panel/回对话记录"""

    def __init__(self, win: _Win):
        super().__init__()
        self._windows = [win]
        self._tab_panel = type("P", (), {"_active_index": 0})()
        self.conv_calls = []

    def _show_conversation_view(self):
        self.conv_calls.append(True)


def test_input_write_tab_mode_resolves_and_back_to_chat(qtbot):
    from app.plugins.capabilities.providers.ui_core import _input_write

    _ensure_qapp()
    win = _Win()
    qtbot.addWidget(win)
    win.input_area.setPlainText("")  # QTextEdit 未显示时初始文档可能带换行，先清空
    host = _TabHost(win)
    qtbot.addWidget(host)

    write = _input_write(host)
    write("请帮我安装 magpie")

    assert win.input_area.toPlainText() == "请帮我安装 magpie"
    assert host.conv_calls == [True]  # 写入后强制回对话视图


def test_input_write_mainwidget_host_uses_tab_window_for_back_to_chat(qtbot, monkeypatch):
    """host 是 MainWidget（窗口级能力池/浮动卡宿主）：回对话入口在 TabManagerWindow 上

    市场卡 git 缺失引导实际走这条：caps_for 解析出的 host 是 MainWidget（有
    input_area，但没有 _show_conversation_view / titleBar），覆盖页由
    TabManagerWindow 托管。不解析 Tab 管理器就会「写进去了但用户还在覆盖页」。
    """
    from app.plugins.capabilities.providers import ui_core

    _ensure_qapp()
    win = _Win()
    qtbot.addWidget(win)
    win.input_area.setPlainText("")

    class _MainWidgetStub(QWidget):
        """只有 input_area 的 MainWidget stub（无回对话入口、无 titleBar）"""

        def __init__(self):
            super().__init__()
            self.input_area = QTextEdit()
            self.input_area.setPlainText("")
            qtbot.addWidget(self.input_area)

    class _TabWindowStub(QWidget):
        def __init__(self, target):
            super().__init__()
            self._windows = [target]
            self.conv_calls = []

        def _show_conversation_view(self):
            self.conv_calls.append(True)

    host = _MainWidgetStub()
    qtbot.addWidget(host)
    tm = _TabWindowStub(win)
    qtbot.addWidget(tm)
    monkeypatch.setattr(ui_core, "_resolve_tab_window", lambda: tm)

    ui_core._input_write(host)("帮我装 git")

    assert host.input_area.toPlainText() == "帮我装 git"  # 写 host 自己的输入框
    assert tm.conv_calls == [True], "MainWidget host 必须经 Tab 管理器切回对话视图"


def test_input_write_back_to_chat_falls_back_to_titlebar(qtbot, monkeypatch):
    """无 _show_conversation_view 时退化到 titleBar 高亮复位（TabManagerWindow 早期形态）"""
    from app.plugins.capabilities.providers import ui_core
    from app.widgets.tab_manager_window import CHAT_TAB_ID

    _ensure_qapp()
    calls = []

    class _TitleBar:
        def set_active_tab(self, tab_id, animate=True):
            calls.append((tab_id, animate))

    class _Host(QWidget):
        def __init__(self):
            super().__init__()
            self.input_area = QTextEdit()
            self.input_area.setPlainText("")
            self.titleBar = _TitleBar()
            qtbot.addWidget(self.input_area)

    host = _Host()
    qtbot.addWidget(host)
    monkeypatch.setattr(ui_core, "_resolve_tab_window", lambda: None)

    ui_core._input_write(host)("x")

    assert calls == [(CHAT_TAB_ID, False)], "退化路径必须复位高亮到对话 tab（不带动画）"


def test_input_write_direct_host_unchanged(qtbot):
    """host 自带 input_area（MainWidget 回退模式）：原语义不变，无 titleBar 不炸"""
    from app.plugins.capabilities.providers.ui_core import _input_write

    _ensure_qapp()
    host = QWidget()
    qtbot.addWidget(host)
    host.input_area = QTextEdit()
    host.input_area.setPlainText("")
    qtbot.addWidget(host.input_area)

    write = _input_write(host)
    write("hello", append=True)
    write("world", append=True)

    assert host.input_area.toPlainText().endswith("hello\nworld")  # append 原语义：前导换行固有


def test_input_write_no_area_raises(qtbot):
    """两处都无输入区：维持显式报错（不静默）"""
    from app.plugins.capabilities.providers.ui_core import _input_write

    _ensure_qapp()
    host = QWidget()
    qtbot.addWidget(host)

    write = _input_write(host)
    try:
        write("x")
        raise AssertionError("应当 raise RuntimeError")
    except RuntimeError as e:
        assert "输入框不可用" in str(e)
