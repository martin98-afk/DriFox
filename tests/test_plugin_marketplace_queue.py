# -*- coding: utf-8 -*-
"""回归测试：插件市场任务调度（per-plugin 串行 + 不同插件并行）

背景：安装/更新/启用/禁用/卸载曾共用单 worker 串行队列——一个
git clone 安装进行中，其他插件的启用/禁用只能排队等待；且历史 bug：
多任务共用槽位时后点任务会 quit 先点任务的线程（静默终止）。

现状：per-plugin 串行（同一插件目录互斥）+ 全局并发上限
（_MAX_CONCURRENT_TASKS）。不同插件任务立即并发执行；
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
    from PySide6.QtWidgets import QApplication

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

    def show(self):
        pass

    def update_search_highlight(self, query):
        pass


def _new_card():
    """构造卡片（不触发 show_card/网络拉取，仅用其队列与行状态逻辑）"""
    from ui.cards import MarketplaceCard

    return MarketplaceCard()


def test_same_plugin_tasks_run_serially(monkeypatch):
    """同一插件多个任务：串行执行，后提交的排队等待，行保持 busy

    同一插件 install/update/enable/disable 操作同一目录，必须互斥：
    p0 运行期间提交的同插件任务入队，完成后自动启动。
    """
    from ui.cards import MarketplaceCard
    from ui.installer import PluginInstaller

    # 固定安装结果：install 恒成功
    monkeypatch.setattr(PluginInstaller, "install", lambda self, meta: True)

    card = _new_card()
    rows = {"p0": FakeRow("p0")}
    card._row_map = rows

    executed = []

    def make_fn(name, delay):
        def fn():
            time.sleep(delay)
            executed.append(name)
            return True

        return fn

    # p0 慢任务先提交：立即启动；同插件 p0b 后提交 → 排队
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
        name="p0",
        fn=make_fn("p0b", 0.05),
        busy_text="安装中…",
        status_text="安装中…",
        status_color="gray",
    )

    # p0 已开始执行，同插件 p0b 排队中
    assert card._task_active is True
    assert len(card._task_queue) == 1, f"同插件任务未排队: {card._task_queue}"
    assert "p0" in card._active_tasks, "首个任务未启动"
    # 任务期间该插件行保持 busy（防重复点击）
    assert rows["p0"]._busy, "任务期间插件行必须保持 busy"

    ok = _wait_until(lambda: not card._task_active and len(executed) == 2)
    assert ok, f"任务未全部完成: executed={executed}"
    assert executed == ["p0", "p0b"], f"同插件任务未串行执行: {executed}"

    card._cleanup_worker()


def test_different_plugin_tasks_run_in_parallel(monkeypatch):
    """不同插件任务：立即并行执行（安装进行中可同时启用/禁用其他插件）

    p0 最慢先提交：立即启动；p1/p2 不同插件 → 不排队，立即各自启动
    （并发上限 3 内）。
    """
    from ui.cards import MarketplaceCard
    from ui.installer import PluginInstaller

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

    card._submit_task(
        kind="install",
        name="p0",
        fn=make_fn("p0", 0.4),
        busy_text="安装中…",
        status_text="安装中…",
        status_color="gray",
    )
    card._submit_task(
        kind="disable",
        name="p1",
        fn=make_fn("p1", 0.05),
        busy_text="禁用中…",
        status_text="禁用中…",
        status_color="gray",
    )
    card._submit_task(
        kind="enable",
        name="p2",
        fn=make_fn("p2", 0.05),
        busy_text="启用中…",
        status_text="启用中…",
        status_color="gray",
    )

    # 三个不同插件任务全部立即启动（无排队）
    assert len(card._task_queue) == 0, f"不同插件任务不应排队: {card._task_queue}"
    assert len(card._active_tasks) == 3, f"不同插件任务未并行: {card._active_tasks}"
    assert card._task_active is True
    # 全部行保持 busy（防重复点击）
    assert all(r._busy for r in rows.values()), "任务期间行必须保持 busy"

    ok = _wait_until(lambda: not card._task_active and len(executed) == 3)
    assert ok, f"任务未全部完成: executed={executed}"
    assert set(executed) == {"p0", "p1", "p2"}

    card._cleanup_worker()


def test_concurrent_cap_queues_overflow(monkeypatch):
    """超过并发上限（3）的任务按 FIFO 等待，完成后自动补位启动"""
    from ui.cards import MarketplaceCard
    from ui.installer import PluginInstaller

    monkeypatch.setattr(PluginInstaller, "install", lambda self, meta: True)

    card = _new_card()
    rows = {f"p{i}": FakeRow(f"p{i}") for i in range(5)}
    card._row_map = rows

    executed = []
    import threading

    done_evt = threading.Event()

    def make_fn(name):
        def fn():
            done_evt.wait(5)
            executed.append(name)
            return True

        return fn

    for i in range(5):
        card._submit_task(
            kind="install",
            name=f"p{i}",
            fn=make_fn(f"p{i}"),
            busy_text="安装中…",
            status_text="安装中…",
            status_color="gray",
        )

    # 3 个立即运行，2 个排队等待槽位
    assert len(card._active_tasks) == 3, f"并发上限未生效: {card._active_tasks}"
    assert len(card._task_queue) == 2, f"超限任务未排队: {card._task_queue}"

    done_evt.set()
    ok = _wait_until(lambda: not card._task_active and len(executed) == 5)
    assert ok, f"排队任务未全部完成: executed={executed}"
    assert set(executed) == {f"p{i}" for i in range(5)}

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


def test_task_fn_prewarms_state_cache(monkeypatch):
    """任务函数包装：执行后后台线程预热 installed/status 缓存

    回归：安装完成回调 _refresh_row_states 主线程全量扫描磁盘（缓存已被
    install invalidate）导致卡顿。修复后状态扫描挪到任务线程（预热），
    主线程回调命中 TTL 缓存不再阻塞。
    """
    import ui.cards as cards_mod
    from ui.installer import PluginInstaller

    calls = {"inst": 0, "status": 0}

    def fake_inst(self, use_cache=True):
        calls["inst"] += 1
        return {}

    def fake_status(self, use_cache=True):
        calls["status"] += 1
        return {}

    monkeypatch.setattr(PluginInstaller, "get_installed_map", fake_inst)
    monkeypatch.setattr(PluginInstaller, "get_status_map", fake_status)

    inner_called = []

    def inner():
        inner_called.append(1)
        return True

    wrapped = cards_mod.MarketplaceCard._wrapped_task_fn(inner)
    assert wrapped() is True, "包装函数必须透传任务结果"
    assert inner_called == [1], "任务函数必须被调用"
    # 预热：任务返回前 installed + status 缓存各扫一次（后台线程，不卡 UI）
    assert calls["inst"] == 1, f"未预热 installed 缓存: {calls}"
    assert calls["status"] == 1, f"未预热 status 缓存: {calls}"


def test_task_fn_prewarm_failure_tolerated(monkeypatch):
    """预热失败不影响任务结果（异常吞掉，主线程回调自行兜底扫描）"""
    import ui.cards as cards_mod
    from ui.installer import PluginInstaller

    def boom(self, use_cache=True):
        raise OSError("disk error")

    monkeypatch.setattr(PluginInstaller, "get_installed_map", boom)
    monkeypatch.setattr(PluginInstaller, "get_status_map", boom)

    def inner():
        return True

    wrapped = cards_mod.MarketplaceCard._wrapped_task_fn(inner)
    assert wrapped() is True, "预热异常不得影响任务结果"
