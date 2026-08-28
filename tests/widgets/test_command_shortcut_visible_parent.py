"""回归测试：命令快捷键父对象必须保持可见，才能在覆盖层（QStackedWidget 隐藏对话区）打开后仍可触发。

对应修复：MainWidget._register_command_shortcuts 将 QShortcut 父对象由 self(MainWidget)
改为 self.window()（顶层窗口）。本测试验证该机制——
父对象不可见时快捷键失效，父对象可见时持续生效。

这正是 '替换(full)' 类卡片"快捷键只能打开、无法关闭"的根因与修复前提：
- 原 bug：父对象 = MainWidget（位于对话区第 0 页），覆盖层打开切页后 MainWidget 被隐藏，
  Qt 的 QShortcutMap 跳过父对象不可见的快捷键 → 第二次按键不触发 → 关不掉。
- 修复：父对象 = 顶层窗口（覆盖层打开时仍可见）→ 快捷键持续生效。
"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import QWidget, QStackedWidget, QApplication
from PySide6.QtTest import QTest
from PySide6.QtGui import QKeySequence


def _make_hierarchy():
    win = QWidget()
    stack = QStackedWidget(win)
    page0 = QWidget(stack)  # 对话区（含 MainWidget）
    page1 = QWidget(stack)  # 覆盖层
    stack.addWidget(page0)
    stack.addWidget(page1)
    win.show()
    QApplication.processEvents()
    return win, stack, page0, page1


def test_shortcut_parented_to_hidden_widget_stops_firing_after_page_switch():
    win, stack, page0, page1 = _make_hierarchy()
    fired = {"hidden_parent": False, "visible_parent": False}

    # 父对象 = page0（对话区）：覆盖层打开后会被隐藏
    qs_hidden = QShortcut(QKeySequence("Ctrl+F"), page0)
    qs_hidden.activated.connect(lambda: fired.__setitem__("hidden_parent", True))
    # 父对象 = win（顶层）：覆盖层打开后仍可见 —— 修复采用的方案
    qs_visible = QShortcut(QKeySequence("Ctrl+G"), win)
    qs_visible.activated.connect(lambda: fired.__setitem__("visible_parent", True))

    # 对话区可见时，两个快捷键都应触发
    QTest.keyClick(win, Qt.Key_F, Qt.ControlModifier)
    QTest.keyClick(win, Qt.Key_G, Qt.ControlModifier)
    QApplication.processEvents()
    assert fired["hidden_parent"] is True
    assert fired["visible_parent"] is True

    # 覆盖层打开：切到 page1，隐藏 page0（对话区）
    stack.setCurrentIndex(1)
    QApplication.processEvents()
    assert not page0.isVisible()

    fired["hidden_parent"] = False
    fired["visible_parent"] = False
    QTest.keyClick(win, Qt.Key_F, Qt.ControlModifier)
    QTest.keyClick(win, Qt.Key_G, Qt.ControlModifier)
    QApplication.processEvents()

    # 父对象不可见 → 失效（正是原 bug 表现）
    assert fired["hidden_parent"] is False
    # 父对象可见（顶层窗口）→ 持续生效（修复后行为）
    assert fired["visible_parent"] is True
