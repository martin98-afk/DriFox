# -*- coding: utf-8 -*-
"""回归测试：插件市场并发安装任务串行化

复现背景：安装/更新/启用/禁用/卸载共用单个 worker 线程槽位，新任务
启动时 `_cleanup_worker()` 强制 quit 旧线程——先点的任务线程可能被
后点的任务打断（静默终止）；且任一任务完成回调 `_refresh_row_states()`
会对所有行 `apply_state()` 无条件清 `_busy`，把其他仍在安装/排队的
行按钮重置回「安装」（用户视角：一个装完，其他待安装的「结束」了）。

修复：任务改为串行队列（一次一个 worker 线程，完成自动启动下一个）；
`_refresh_row_states()` 跳过 busy 行；市场拉取与任务 worker 分离。
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))


def _wait_until(pred, timeout=6.0, interval=0.02):
    """轮询等待谓词成立（pump 事件循环）"""
    deadline = time.time() + timeout
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    while time.time() < deadline:
        if app is not None:
            app.processEvents()
        if pred():
            return True
        time.sleep(interval)
    return False


class FakeRow:
    """轻量行桩：模拟 _PluginRow 的 busy 状态机（验证队列/busy 保护逻辑）"""

    def __init__(self, name: str):
        self._meta = {"name": name, "version": "1.0.0"}
        self._name = name
        self._busy = False
        self._busy_text = ""
        self._installed = False

    def _update_btn_text(self):
        pass

    def apply_state(self, installed, has_update, local_version, status=""):
        self._installed = installed
        self._busy = False  # 与真实 _PluginRow 一致：无条件清 busy（被测保护点）

    def setVisible(self, visible):
        pass

    def update_search_highlight(self, query):
        pass


def _new_card():
    """构造卡片（不触发 show_card/网络拉取，仅用其队列与行状态逻辑）"""
    from ui.cards import MarketplaceCard

    return MarketplaceCard()


def test_tasks_run_serially(monkeypatch):
    """多个安装任务：串行执行，一个完成自动启动下一个，行保持 busy"""
    from ui.cards import MarketplaceCard
    from ui.installer import PluginInstaller

    # 固定安装结果：install 恒成功
    monkeypatch.setattr(PluginInstaller, "install", lambda self, meta: True)

    card = _new_card()
    rows = {f"p{i}": FakeRow(f"p{i}") for i in range(3)}
    card._row_map = rows

    executed = []

    def make_fn(name, delay):
        def fn():
            time.sleep(delay)
            executed.append(name)
            return True

        return fn

    # p0 最慢：提交时 p0 立即开始跑，p1/p2 入队等待
    card._submit_task(
        kind="install",
        name="p0",
        fn=make_fn("p0", 0.3),
        busy_text="安装中…",
        status_text="安装中…",
        status_color="gray",
    )
    card._submit_task(
        kind="install",
        name="p1",
        fn=make_fn("p1", 0.05),
        busy_text="安装中…",
        status_text="安装中…",
        status_color="gray",
    )
    card._submit_task(
        kind="install",
        name="p2",
        fn=make_fn("p2", 0.05),
        busy_text="安装中…",
        status_text="安装中…",
        status_color="gray",
    )

    # p0 已开始执行，p1/p2 排队中
    assert card._task_active is True
    assert len(card._task_queue) == 2
    # 全部行在任务期间保持 busy（防重复点击）
    assert all(r._busy for r in rows.values()), "任务排队期间行必须保持 busy"

    ok = _wait_until(lambda: not card._task_active and len(executed) == 3)
    assert ok, f"任务未全部完成: executed={executed}"
    assert executed == ["p0", "p1", "p2"], f"任务未串行执行: {executed}"

    card._cleanup_worker()


def test_completion_does_not_reset_queued_rows(monkeypatch):
    """一个任务完成刷新行状态时，不得重置其他排队中任务的 busy 行

    回归：安装 A 完成 → _refresh_row_states → B（安装中/排队）按钮恢复
    「安装」= 用户看到的「一个装完其他待安装的结束了」。
    """
    from ui.installer import PluginInstaller

    # 本地安装状态恒空（A 装完但 B/C 尚未装完的中间态）
    monkeypatch.setattr(PluginInstaller, "get_installed_map", lambda self, use_cache=True: {})
    monkeypatch.setattr(PluginInstaller, "get_status_map", lambda self, use_cache=True: {})

    card = _new_card()
    rows = {f"p{i}": FakeRow(f"p{i}") for i in range(3)}
    card._row_map = rows

    # 模拟：p0 安装中，p1/p2 排队中（全部 busy）
    for r in rows.values():
        r._busy = True
        r._busy_text = "安装中…"

    # p0 完成 → 任务分发（含 _release_row_busy + _refresh_row_states 的 busy 跳过）
    card._on_task_done({"kind": "install", "name": "p0"}, True)

    # p1/p2 仍在队列等待 → busy 必须保留（不能被重置回「安装」）
    assert rows["p1"]._busy, "完成回调把排队任务的 busy 清掉了（bug 回归）"
    assert rows["p2"]._busy, "完成回调把排队任务的 busy 清掉了（bug 回归）"
    # p0 自身行应退出 busy（任务已完成）
    assert not rows["p0"]._busy

    card._cleanup_worker()
