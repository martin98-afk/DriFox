# -*- coding: utf-8 -*-
"""复现：工作台 per-tab 显隐/页签记忆在延迟刷新后的同步回归。

场景：A(工作台开·产物页) / B(工作台开·历史页)，来回切换验证页签跟随。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QEventLoop, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication, QWidget


class FakeChatWindow(QWidget):
    """最小对话窗口 stub：带 add_window 所需信号"""

    ai_state_changed = pyqtSignal(str)

    def __init__(self, wid):
        super().__init__()
        self._window_id = wid
        self._current_project = ""

    def _notify_history_data_changed(self, broadcast=True):
        pass

    def _build_ui_context(self):
        return {}


def drain(n_rounds=5):
    """跑 n 轮事件循环，确保 singleShot(0) 全部执行"""
    for _ in range(n_rounds):
        loop = QEventLoop()
        QTimer.singleShot(10, loop.quit)
        loop.exec_()


def switch_tab(tm, index):
    # 模拟用户点击对话标签：TabPanel 选中态 + 宿主 _on_tab_selected
    tm._tab_panel.set_active_index(index)
    tm._on_tab_selected(index)


def main():
    app = QApplication.instance() or QApplication([])

    from app.tray_manager import TrayManager
    from app.widgets.tab_manager_window import TabManagerWindow

    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    tm = TabManagerWindow.create_instance()
    tm.show()

    # 两个假对话窗口（只需 _window_id/_workbench_* 记忆属性）
    win_a, win_b = FakeChatWindow("A"), FakeChatWindow("B")
    win_a._window_id = "A"
    win_b._window_id = "B"
    idx_a = tm.add_window(win_a)
    idx_b = tm.add_window(win_b)
    print(f"windows: A={idx_a} B={idx_b}")
    print(f"active_index={tm._tab_panel.active_index} cur_win={tm.get_current_window()}")

    panel = tm.workbench_panel

    # ── 初始：切到 A，打开工作台，选产物页 ──
    switch_tab(tm, idx_a)
    drain()
    tm.set_workbench_visible(True, animate=False)
    drain()
    panel.set_current_tab(panel.TAB_ARTIFACTS, user=True)
    drain()
    print(f"[A init] visible={tm.is_workbench_visible()} tab_id={panel._tab_id_at(panel.current_tab())}")
    print(f"[A mem] {(win_a._workbench_visible_memory, win_a._workbench_tab_memory)}")

    # ── 切到 B：打开工作台，选历史页 ──
    switch_tab(tm, idx_b)
    drain()
    tm.set_workbench_visible(True, animate=False)
    drain()
    # 历史页需挂载才存在
    hist = QWidget()
    panel.attach_history_page(hist)
    panel.set_current_tab(panel.TAB_HISTORY, user=True)
    drain()
    print(f"[B init] visible={tm.is_workbench_visible()} tab_id={panel._tab_id_at(panel.current_tab())}")
    print(f"[B mem] {(win_b._workbench_visible_memory, win_b._workbench_tab_memory)}")

    # ── 来回切换验证 ──
    switch_tab(tm, idx_a)
    drain()
    state = (tm.is_workbench_visible(), panel._tab_id_at(panel.current_tab()))
    print(f"[switch->A] visible={state[0]} tab={state[1]}  expect=(True, artifacts)")
    ok1 = state == (True, "artifacts")

    switch_tab(tm, idx_b)
    drain()
    state = (tm.is_workbench_visible(), panel._tab_id_at(panel.current_tab()))
    print(f"[switch->B] visible={state[0]} tab={state[1]}  expect=(True, history)")
    ok2 = state == (True, "history")

    # ── 显隐独立记忆：A 关工作台 → B 应仍开着 ──
    switch_tab(tm, idx_a)
    drain()
    tm.set_workbench_visible(False, animate=False)
    drain()
    print(f"[A off] visible={tm.is_workbench_visible()}")
    switch_tab(tm, idx_b)
    drain()
    state = (tm.is_workbench_visible(), panel._tab_id_at(panel.current_tab()))
    print(f"[switch->B] visible={state[0]} tab={state[1]}  expect=(True, history)")
    ok3 = state[0] is True

    switch_tab(tm, idx_a)
    drain()
    print(f"[switch->A] visible={tm.is_workbench_visible()}  expect=False")
    ok4 = tm.is_workbench_visible() is False

    # ── 场景 C：快速连续切换（模拟真实点击节奏，中间不等待）──
    # 重新初始化：A 开·产物页，B 开·历史页
    switch_tab(tm, idx_b)
    drain()
    tm.set_workbench_visible(True, animate=False)
    drain()
    switch_tab(tm, idx_a)
    drain()
    tm.set_workbench_visible(True, animate=False)
    drain()
    panel.set_current_tab(panel.TAB_ARTIFACTS, user=True)
    drain()
    print(f"[rapid init] A mem={getattr(win_a, '_workbench_tab_memory', None)} B mem={getattr(win_b, '_workbench_tab_memory', None)}")

    # 连续快速切换：A→B→A→B，中间不 drain（事件循环来不及跑延迟任务）
    switch_tab(tm, idx_b)
    switch_tab(tm, idx_a)
    switch_tab(tm, idx_b)
    switch_tab(tm, idx_a)
    drain()
    state_a = (tm.is_workbench_visible(), panel._tab_id_at(panel.current_tab()))
    print(f"[rapid->A] visible={state_a[0]} tab={state_a[1]}  expect=(True, artifacts)")
    ok5 = state_a == (True, "artifacts")

    switch_tab(tm, idx_b)
    drain()
    state_b = (tm.is_workbench_visible(), panel._tab_id_at(panel.current_tab()))
    print(f"[rapid->B] visible={state_b[0]} tab={state_b[1]}  expect=(True, history)")
    ok6 = state_b == (True, "history")

    print()
    ok_all = all([ok1, ok2, ok3, ok4, ok5, ok6])
    print(f"tab_follow_A={ok1} tab_follow_B={ok2} visible_B_indep={ok3} visible_A_indep={ok4} rapid_A={ok5} rapid_B={ok6}")
    print("RESULT:", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
