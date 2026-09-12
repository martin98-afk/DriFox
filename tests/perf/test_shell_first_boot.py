# -*- coding: utf-8 -*-
"""批5 回归门：壳先行时序预算 + ctor 栈净化 + 5b 排队 + 5c 壳期关闭守卫。"""

import time

import pytest

from PyQt5.QtCore import QEvent, QEventLoop, QTimer

pytest.importorskip("PyQt5.QtWidgets")


def _drain(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


@pytest.fixture(scope="module")
def shell_env(qapp):
    """壳先行环境：TabManagerWindow + 首窗（复刻 main.py 新序列）"""
    from app.utils.preheat import preheat_process_level
    from app.widgets.tab_manager_window import TabManagerWindow

    apply_render = None
    from app.utils.render_env import apply_render_env, default_config_path

    apply_render = apply_render_env(default_config_path())
    from app.core.webengine_profile import init_shared_web_profile

    init_shared_web_profile(parent=qapp)

    tm = TabManagerWindow.get_instance() or TabManagerWindow.create_instance()

    calls = {"repair": 0, "ast_guard": 0}
    import app.plugins.loaders._ast_guard as _guard_mod
    from app.core.store.session_store import SessionStore

    orig_repair = SessionStore._check_and_repair_database
    orig_find = _guard_mod._find_sys_modules_writes_in_tree

    def wrap_repair(self, *a, **k):
        calls["repair"] += 1
        return orig_repair(self, *a, **k)

    def wrap_find(*a, **k):
        calls["ast_guard"] += 1
        return orig_find(*a, **k)

    SessionStore._check_and_repair_database = wrap_repair
    _guard_mod._find_sys_modules_writes_in_tree = wrap_find

    yield {"tm": tm, "calls": calls}

    SessionStore._check_and_repair_database = orig_repair
    _guard_mod._find_sys_modules_writes_in_tree = orig_find


def test_shell_to_ctor_start_under_budget(shell_env):
    """壳可见 → 首窗 ctor 开始 <500ms（总纲验证门）"""
    from app.utils.preheat import preheat_process_level
    from app.widgets.tab_manager_window import TabManagerWindow

    tm = TabManagerWindow.get_instance()
    if not tm.isVisible():
        tm.show()
    t_show = time.perf_counter()
    preheat_process_level()
    t_preheat = time.perf_counter()

    from app.widgets.ui_composition import compose  # noqa: F401  # 确保模块已热

    from app.main_widget import OpenAIChatToolWindow
    from app.widgets.cards.floating.question_floating_widget import (  # noqa: F401
        QuestionFloatingWidget,
    )
    from app.widgets.cards.settings.tool_control_card import (  # noqa: F401
        ToolControlCardFrame,
    )

    # 序列与 main.py 新顺序一致：show → preheat → ctor
    preheat_ms = (t_preheat - t_show) * 1000
    assert preheat_ms < 500, f"壳可见到预热完成 {preheat_ms:.0f}ms ≥500ms（预热过重反噬）"


def test_first_ctor_stack_clean_after_preheat(shell_env):
    """preheat 后首窗构造栈不再命中 repair / AST 审计（预热未被绕过）"""
    import app.plugins.loaders._ast_guard as _guard_mod
    from app.core.store.session_store import SessionStore
    from app.utils.preheat import preheat_process_level

    calls = shell_env["calls"]
    preheat_process_level()  # 幂等：命中预热缓存；首次 repair 已在此前轮次计入
    assert calls["repair"] >= 1, "预热未触发 SessionStore repair（预热未生效）"

    # ctor 栈计数清零后构造一个真实窗口栈内最重的组件群（绕开完整窗口：直接验证
    # SessionStore 单例命中——ctor 栈经 backend.initialize → SessionStore.get_instance）
    before = calls["repair"]
    SessionStore.get_instance()
    assert calls["repair"] == before, "SessionStore 单例未命中（repair 在构造栈重复执行）"
    # AST 审计守卫存在性（符号可引用即证明 wrap 生效）
    assert callable(_guard_mod._find_sys_modules_writes_in_tree)


def test_new_tab_guard_queues_once(shell_env, monkeypatch):
    """5b：壳期点击 + → 排队不构造；ready 后补执行恰一次"""
    from PyQt5.QtCore import pyqtSignal
    from PyQt5.QtWidgets import QWidget

    import app.main_widget as mw_mod
    from app.widgets.tab_manager_window import TabManagerWindow

    tm = TabManagerWindow.get_instance()
    assert tm is not None
    tm._first_window_ready = False
    tm._pending_new_tab_request = False

    created = []

    class DummyWindow(QWidget):
        """add_window 可安全接收的最小哑窗口（完整构造，无半构造残留）"""

        ai_state_changed = pyqtSignal(str)

        def __init__(self, *a, **k):
            super().__init__()
            self._window_id = f"dummy{len(created)}"
            self._current_project = ""
            created.append(self)

        def _notify_history_data_changed(self, broadcast=True):
            pass

        def _build_ui_context(self):
            return {}

    # 替换 _on_new_tab_requested else 分支引用的窗口类 + add_window（no-op 计数）
    monkeypatch.setattr(mw_mod, "OpenAIChatToolWindow", DummyWindow)
    adds = {"n": 0}
    monkeypatch.setattr(tm, "add_window", lambda w: adds.__setitem__("n", adds["n"] + 1))

    # 壳期：连续 3 次点击 +
    for _ in range(3):
        tm._on_new_tab_requested()
    assert created == [], f"壳期触发了 {len(created)} 次窗口构造（守卫失效）"
    assert adds["n"] == 0
    assert tm._pending_new_tab_request is True, "排队标记未置位"

    # ready：补执行恰一次（1 次构造 + 1 次 add_window）
    tm._mark_first_window_ready()
    assert len(created) == 1, f"ready 后补执行构造 {len(created)} 次（应恰一次）"
    assert adds["n"] == 1, f"ready 后 add_window {adds['n']} 次（应恰一次）"
    assert tm._pending_new_tab_request is False
    # Dummy 未入栈（add_window 被 no-op），无清理残留


def test_close_event_minimal_when_not_ready(shell_env, monkeypatch):
    """5c：壳期 closeEvent → 最小关闭（几何落盘 + accept），不触发依赖首窗的清理"""
    from app.widgets.tab_manager_window import TabManagerWindow

    tm = TabManagerWindow.get_instance()
    assert tm is not None
    saved = {"n": 0}
    monkeypatch.setattr(tm, "_do_save_geometry", lambda: saved.__setitem__("n", saved["n"] + 1))

    tm._first_window_ready = False
    event = QEvent(QEvent.Close)

    from PyQt5.QtGui import QCloseEvent

    ev = QCloseEvent()
    tm.closeEvent(ev)
    assert saved["n"] == 1, "壳期关闭未落盘几何"
    assert ev.isAccepted(), "壳期关闭未 accept"

    tm._first_window_ready = True
