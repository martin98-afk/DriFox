# -*- coding: utf-8 -*-
"""批2 回归门：refresh_workspace_tree 250ms 防抖 + force 立即执行。

清单：连续新建/关闭 tab 时整树重建合并为一次（≤2 次）；force=True 跳过防抖。
"""

import pytest

from PyQt5.QtCore import QEventLoop, QTimer

pytest.importorskip("PyQt5.QtWidgets")


def _drain(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


class _StubPanel:
    def __init__(self, mode="tree"):
        self._mode = mode
        self.refresh_calls = 0

    def current_mode(self):
        return self._mode

    def refresh_tree(self):
        self.refresh_calls += 1


def _make_tm():
    """轻量 TabManagerWindow：__new__ 绕过完整 __init__，补跑 QObject 初始化
    （QTimer(self) 需要有效的 QObject 身份）后手工注入依赖"""
    from PyQt5.QtCore import QObject

    from app.widgets.tab_manager_window import TabManagerWindow

    tm = TabManagerWindow.__new__(TabManagerWindow)
    QObject.__init__(tm)
    tm._tab_panel = _StubPanel()
    return tm


def test_debounce_merges_burst(qapp):
    """连续 5 次 tab 数量变化 → 250ms 防抖窗口内合并为 1 次刷新"""
    tm = _make_tm()
    for _ in range(5):
        tm._on_tab_count_changed_for_tree(1)
    assert tm._tab_panel.refresh_calls == 0, "防抖未生效：首次触发立即刷新"
    _drain(350)
    assert tm._tab_panel.refresh_calls == 1, f"连续 5 次触发刷了 {tm._tab_panel.refresh_calls} 次（应合并为 1）"


def test_refresh_default_within_budget(qapp):
    """连续 spawn 场景模拟：多次调用后刷新次数 ≤2"""
    tm = _make_tm()
    for _ in range(5):
        tm.refresh_workspace_tree()
        _drain(30)  # 间隔 <250ms：全部落在同一防抖窗口
    _drain(350)
    assert tm._tab_panel.refresh_calls <= 2, f"5 次调用刷了 {tm._tab_panel.refresh_calls} 次（>2）"


def test_force_bypasses_debounce(qapp):
    """force=True 跳过防抖立即执行"""
    tm = _make_tm()
    tm.refresh_workspace_tree(force=True)
    assert tm._tab_panel.refresh_calls == 1, "force=True 未立即执行"
    # 先排队一个防抖，再 force：force 立即生效，防抖到期仍会补一次（合并语义不破坏）
    tm.refresh_workspace_tree()
    tm.refresh_workspace_tree(force=True)
    assert tm._tab_panel.refresh_calls == 2, "force=True 未立即执行（第二次）"
    _drain(350)


def test_missing_panel_no_throw(qapp):
    """panel 缺失/无 refresh_tree 时不抛（原守卫语义保持）"""
    tm = _make_tm()
    tm._tab_panel = None
    tm.refresh_workspace_tree(force=True)
    tm._on_tab_count_changed_for_tree(1)
