# -*- coding: utf-8 -*-
"""复现测试：Tab 模式共享顶层窗口时命令快捷键归属权缺陷。

机制推演（app/main_widget.py）：
- _register_command_shortcuts 以类级 _window_shortcut_cache 按 win_id 去重：
  同窗口只有第一个注册的 tab 实例持有真实 QShortcut，其余 tab 全部跳过。
- closeEvent 关闭持有者 tab：_disconnect_command_shortcut_cleanup 先行
  deleteLater 全部 QShortcut → 注销自身 → 尾部清理块以 `remaining <= 1`
  （remaining 为存活兄弟数，self 已注销不计）判定"最后一个实例"并 pop 缓存，
  但从不触发存活 tab 的重新注册。
- H1：持有者关闭后，窗口上已无任何存活快捷键，且常规路径无人重建 →
  存活 tab 的命令快捷键持续失效（直到新建 tab / 插件热重载）。
- H2：非持有者关闭同样 pop 缓存 → 之后新 tab 注册出第二套 QShortcut →
  Qt 同 context 重复 key sequence 行为 undefined → 开关型命令被执行两次，
  用户视角即"按了没反应"。

两条契约（修复前红、修复后绿）：
1. test_holder_closed_window_must_still_have_live_shortcut
2. test_non_holder_closed_then_new_register_no_duplicate
"""

import types

import pytest
from PyQt5 import sip
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QApplication, QWidget

from app.core import window_registry as window_registry_mod
from app.core.command_manager import CommandManager, CommandType


TEST_CMD = "shortcut_probe_cmd"
TEST_SEQ = "Ctrl+Alt+9"


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


def _make_stub_tab(shared_parent: QWidget):
    """绕开重型 __init__，仅初始化 Qt 基类部分的最小 stub tab。

    _register_command_shortcuts / _disconnect_command_shortcut_cleanup 均为
    MethodType 绑定的真实生产方法；handler 依赖注入 spy 替身。
    """
    from app.main_widget import OpenAIChatToolWindow

    class _StubTab(OpenAIChatToolWindow):
        def __init__(self):
            QWidget.__init__(self, shared_parent)

        # 屏蔽完整 UI 初始化钩子：对象级断言不依赖窗口可见性
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


def _register(inst) -> int:
    """调用真实注册方法（ensure 幂等），返回执行前窗口缓存的增量数"""
    from app.main_widget import OpenAIChatToolWindow

    before = len(OpenAIChatToolWindow._window_shortcut_cache.get(id(inst.window() or inst), []))
    OpenAIChatToolWindow._register_command_shortcuts(inst)
    after = len(OpenAIChatToolWindow._window_shortcut_cache.get(id(inst.window() or inst), []))
    return after - before


def _window_cache(win: QWidget) -> list:
    from app.main_widget import OpenAIChatToolWindow

    return OpenAIChatToolWindow._window_shortcut_cache.get(id(win), [])


def _simulate_close_tail(closing_inst):
    """历史占位：closeEvent 尾部窗口级 cache 清理块已随归属权修复删除。

    保留函数签名使两条契约的场景描述与修复前后一一对应；
    closeEvent 现在不再触碰 _window_shortcut_cache（由
    shortcut_parent.destroyed 钩子统一管理条目生命周期）。
    """


def _live_seq_shortcuts(win: QWidget, seq: str) -> int:
    """统计窗口缓存的共享 QShortcut 中存活的指定键序列数量"""
    app = QApplication.instance()
    assert app is not None
    app.processEvents()
    target = QKeySequence(seq)[0]
    count = 0
    for qs in _window_cache(win):
        try:
            if sip.isdeleted(qs):
                continue
            if qs.key()[0] == target:
                count += 1
        except RuntimeError:
            continue
    return count


def _close_tab(inst):
    """模拟关 tab 的真实顺序：清理钩子 → 从注册表注销 → 尾部 cache 清理"""
    from app.main_widget import OpenAIChatToolWindow

    OpenAIChatToolWindow._disconnect_command_shortcut_cleanup(inst)
    window_registry_mod.unregister_window(inst)
    _simulate_close_tail(inst)


def test_holder_closed_window_must_still_have_live_shortcut(isolated_env):
    """H1：持有者 tab 关闭后，同窗存活 tab 必须仍持有可用快捷键。

    修复前行为：持有者的 QShortcut 被 deleteLater，去重缓存被 pop 却无人触发
    重注册 → 存活 tab 手上一条快捷键都没有（直到新建 tab / 插件热重载）。
    """
    win = QWidget()

    holder = _make_stub_tab(win)
    survivor = _make_stub_tab(win)
    for t in (holder, survivor):
        window_registry_mod.register_window(t)

    assert _register(holder) == 1, "首次注册应向窗口缓存创建 QShortcut"
    assert _register(survivor) == 0, "幂等 ensure：后续注册不应重复创建"

    _close_tab(holder)

    live = _live_seq_shortcuts(win, TEST_SEQ)
    assert live >= 1, f"持有者关闭后窗口仅剩 {live} 条可用快捷键 —— 命令快捷键失效且无自愈路径（H1 复现）"


def test_non_holder_closed_then_new_register_no_duplicate(isolated_env):
    """H2：非持有者 tab 关闭（pop 缓存）后，新 tab 再注册不得产生重复。"""
    win = QWidget()

    holder = _make_stub_tab(win)
    other = _make_stub_tab(win)
    for t in (holder, other):
        window_registry_mod.register_window(t)

    assert _register(holder) == 1
    assert _register(other) == 0  # 非持有者，注册被去重跳过

    _close_tab(other)  # 自身无 QShortcut 可删，但会错误 pop 掉去重缓存

    newcomer = _make_stub_tab(win)
    window_registry_mod.register_window(newcomer)
    _register(newcomer)  # 缓存已空 → 又注册一套

    total = _live_seq_shortcuts(win, TEST_SEQ)
    assert total <= 1, (
        f"同一窗口键序列 {TEST_SEQ} 同时存在 {total} 条 QShortcut"
        " —— Qt 行为 undefined，开关型命令将被执行两次（H2 复现）"
    )
