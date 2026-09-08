# -*- coding: utf-8 -*-
"""回归：命令热重载后窗口上不得残留重复 QShortcut。

故障机理（2026-09-08）：
- QShortcut 的 Qt 父对象是顶层窗口，Python wrapper 被回收不会带走 C++ 对象。
- 旧实现在热重载时只做 `_window_shortcut_cache.clear()`，C++ QShortcut 仍留在
  Qt 的 QShortcutMap 里；随后重建又注册一套同键序列的 QShortcut。
- 同窗口 + 同键序列多套 → Qt 判定 ambiguous，只发 activatedAmbiguously()；
  而处理器只连了 activated() → 全部命令快捷键静默失效，只能重启恢复。

契约（修复前红、修复后绿）：
1. test_hot_reload_rebind_keeps_single_live_shortcut —— 窗口上标记的 QShortcut 恒为 1。
2. test_hot_reload_rebind_shortcut_still_fires       —— 多次重建后按键仍能触发 activated。
"""

import types

import pytest
from PyQt5 import sip
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QMainWindow, QShortcut
from PyQt5.QtTest import QTest

from app.core import window_registry as window_registry_mod
from app.core.command_manager import CommandManager, CommandType

TEST_CMD = "hot_reload_probe_cmd"
TEST_SEQ = "Ctrl+Alt+7"


@pytest.fixture()
def isolated_env(qapp):
    """隔离 CommandManager 单例 + window_registry + 窗口级快捷键缓存"""
    from app.main_widget import OpenAIChatToolWindow

    CommandManager.reset_instance()
    mgr = CommandManager.get_instance()
    mgr.register(
        name=TEST_CMD,
        command_type=CommandType.FUNCTION,
        description="test",
        shortcut=TEST_SEQ,
    )

    old_instances = [r for r in window_registry_mod.window_instances if r() is not None]
    window_registry_mod.window_instances.clear()
    OpenAIChatToolWindow._window_shortcut_cache.clear()

    yield mgr

    for name in list(mgr.get_command_names()):
        mgr.unregister(name)
    window_registry_mod.window_instances[:] = list(old_instances)
    OpenAIChatToolWindow._window_shortcut_cache.clear()
    CommandManager.reset_instance()


def _make_stub_tab(shared_parent):
    """绕开重型 __init__ 的最小 stub tab（生产方法 MethodType 绑定）"""
    from app.main_widget import OpenAIChatToolWindow

    class _StubTab(OpenAIChatToolWindow):
        def __init__(self):
            QApplication.instance().processEvents()
            from PyQt5.QtWidgets import QWidget

            QWidget.__init__(self, shared_parent)

        def showEvent(self, event):
            event.accept()

        def resizeEvent(self, event):
            event.accept()

    inst = _StubTab()
    inst._is_destroyed = False
    inst.fired_log = []
    inst._command_has_params = types.MethodType(lambda self, n: False, inst)
    inst._has_command_handler = types.MethodType(lambda self, n: True, inst)
    inst._execute_command = types.MethodType(lambda self, n: inst.fired_log.append((id(self), n)), inst)
    return inst


def _live_marked_shortcuts(win) -> list:
    """统计窗口上「带命令标记且未被 C++ 删除」的 QShortcut（含缓存外孤儿）"""
    from app.main_widget import OpenAIChatToolWindow

    app = QApplication.instance()
    assert app is not None
    app.processEvents()
    marker = OpenAIChatToolWindow._COMMAND_SHORTCUT_MARKER
    out = []
    for qs in win.findChildren(QShortcut):
        try:
            if sip.isdeleted(qs):
                continue
            if qs.property(marker):
                out.append(qs)
        except RuntimeError:
            continue
    return out


def _rebind_like_hot_reload():
    """走真实热重载入口：builtin_commands._rebind_command_shortcuts()"""
    from app.core.builtin_commands import _rebind_command_shortcuts

    _rebind_command_shortcuts()


def test_hot_reload_rebind_keeps_single_live_shortcut(isolated_env):
    """多次命令热重载后，窗口上同一键序列的 QShortcut 必须恒为 1 条。

    修复前：clear() 只丢 Python 引用，C++ 对象残留 → 每轮重载 +1 条 →
    Qt 判 ambiguous → activated() 永不触发（快捷键全灭）。
    """
    win = QMainWindow()
    tab = _make_stub_tab(win)
    window_registry_mod.register_window(tab)

    from app.main_widget import OpenAIChatToolWindow

    OpenAIChatToolWindow._register_command_shortcuts(tab)
    assert len(_live_marked_shortcuts(win)) == 1, "首次注册应恰好 1 条"

    for i in range(3):
        _rebind_like_hot_reload()
        live = _live_marked_shortcuts(win)
        assert len(live) == 1, f"第 {i + 1} 轮热重载后窗口上残留 {len(live)} 条命令快捷键 —— 将触发 ambiguous 导致快捷键全灭"


def test_hot_reload_rebind_shortcut_still_fires(isolated_env):
    """多次重建后按键仍必须触发 activated（ambiguous 时该信号永不发射）。"""
    win = QMainWindow()
    win.resize(400, 300)
    win.show()
    QApplication.instance().processEvents()

    # _resolve_target(parent) 需要顶层窗口具备命令入口，否则快捷键空转
    fired = []
    win._command_has_params = lambda n: False
    win._has_command_handler = lambda n: True
    win._execute_command = lambda n: fired.append(n)

    tab = _make_stub_tab(win)
    window_registry_mod.register_window(tab)

    from app.main_widget import OpenAIChatToolWindow

    OpenAIChatToolWindow._register_command_shortcuts(tab)

    for _ in range(2):
        _rebind_like_hot_reload()

    QTest.keyClick(win, Qt.Key_7, Qt.ControlModifier | Qt.AltModifier)
    QApplication.instance().processEvents()

    assert fired == [TEST_CMD], f"热重载重建后快捷键未触发 activated（实际触发 {fired}）"
    win.close()
