# -*- coding: utf-8 -*-
"""插件市场「行内进度条 + 取消按钮」的 UI 层回归测试

覆盖：
1. 安装/更新提交后按钮文案变「取消」且保持可点（可取消阶段）
2. 进度回调驱动细进度条显隐与数值（None=不确定进度 → 忙碌滚动条）
3. 点「取消」把运行中任务的 token 置位；排队中任务直接出队
4. 取消完成后行退出 busy、进度条隐藏、不写失败记录
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))


def _pump(seconds=0.2):
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    deadline = time.time() + seconds
    while time.time() < deadline:
        if app is not None:
            app.processEvents()
        time.sleep(0.01)


def _wait_until(pred, timeout=6.0, interval=0.02):
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if app is not None:
            app.processEvents()
        if pred():
            return True
        time.sleep(interval)
    return False


def _new_card():
    from ui.cards import MarketplaceCard

    card = MarketplaceCard()
    return card


def _meta(name="demo"):
    return {"name": name, "version": "1.0.0", "description": "x"}


def test_install_button_shows_cancel(monkeypatch):
    """提交安装后按钮文案为「取消」且可点（用户能中途取消）"""
    from ui.cards import _PluginRow
    from ui.installer import PluginInstaller

    # 拦截真实安装：只要被调用就阻塞，便于观察 busy 态
    gate = {"evt": None}
    import threading

    evt = threading.Event()
    gate["evt"] = evt
    monkeypatch.setattr(PluginInstaller, "install", lambda self, m: (evt.wait(5), True)[1])

    card = _new_card()
    row = _PluginRow(_meta("demo"), installed=False)
    card._row_map["demo"] = row

    card._async_install(_meta("demo"))
    _pump(0.05)

    assert row._busy, "提交后行应处于 busy"
    assert row._cancellable, "安装/更新阶段应标记为可取消"
    assert row._btn.text() == "取消", f"按钮文案应为「取消」，实际 {row._btn.text()!r}"
    assert row._btn.isEnabled(), "取消按钮必须可点"

    evt.set()
    assert _wait_until(lambda: not card._task_active), "任务未结束"
    card._cleanup_worker()


def test_progress_updates_bar(monkeypatch):
    """进度回调驱动进度条：确定进度设数值，None 切换不确定模式"""
    from ui.cards import _PluginRow

    row = _PluginRow(_meta("demo"), installed=False)
    row.set_downloading()

    assert row._progress.isHidden(), "初始应隐藏进度条"

    row.set_progress(0.42, "下载中 42%")
    assert not row._progress.isHidden(), "收到进度后必须显示进度条"
    assert row._progress.value() == 42
    assert row._progress.maximum() == 100

    row.set_progress(None, "解压中")
    assert row._progress.maximum() == 0, "None 应切不确定进度（忙碌滚动）"

    row.clear_progress()
    assert row._progress.isHidden(), "clear_progress 必须隐藏进度条"
    assert row._progress.value() == 0


def test_cancel_running_task_sets_token(monkeypatch):
    """点取消：运行中任务的 token 被置位，按钮转「取消中…」不可再点"""
    from ui.cards import _PluginRow
    from ui.installer import PluginInstaller
    import threading

    evt = threading.Event()
    monkeypatch.setattr(PluginInstaller, "install", lambda self, m: (evt.wait(5), True)[1])

    card = _new_card()
    row = _PluginRow(_meta("demo"), installed=False)
    card._row_map["demo"] = row

    card._async_install(_meta("demo"))
    _pump(0.05)
    task = card._active_tasks.get("demo")
    assert task is not None, "任务未进入活跃表"

    card._cancel_task("demo")
    _pump(0.02)

    assert task["token"].cancelled, "取消必须置位 token"
    assert row._busy_text == "取消中…", f"按钮应转「取消中…」，实际 {row._busy_text!r}"
    assert not row._cancellable, "取消中不应再提供取消入口"

    evt.set()
    _wait_until(lambda: not card._task_active)
    card._cleanup_worker()


def test_cancel_queued_task_removes_it(monkeypatch):
    """排队中的任务取消：直接出队并解除行 busy（无需等 worker）"""
    from ui.cards import _PluginRow
    from ui.installer import PluginInstaller
    import threading

    evt = threading.Event()
    monkeypatch.setattr(PluginInstaller, "install", lambda self, m: (evt.wait(5), True)[1])

    card = _new_card()
    card._MAX_CONCURRENT_TASKS = 1  # 逼后续任务排队
    rows = {}
    for n in ("a", "b"):
        rows[n] = _PluginRow(_meta(n), installed=False)
        card._row_map[n] = rows[n]

    card._async_install(_meta("a"))
    card._async_install(_meta("b"))
    _pump(0.05)

    assert any(t["name"] == "b" for t in card._task_queue), "b 应排队"
    card._cancel_task("b")
    _pump(0.02)

    assert not any(t["name"] == "b" for t in card._task_queue), "排队任务必须出队"
    assert not rows["b"]._busy, "取消排队任务后行应退出 busy"

    evt.set()
    _wait_until(lambda: not card._task_active)
    card._cleanup_worker()


def test_cancelled_task_not_recorded_as_failure(monkeypatch):
    """用户取消不得写进「下载/更新记录」的失败条目，且行退出 busy"""
    from ui.cards import _PluginRow

    card = _new_card()
    row = _PluginRow(_meta("demo"), installed=False)
    card._row_map["demo"] = row
    row.set_downloading()

    recorded = []
    monkeypatch.setattr(card, "_record_task_result", lambda t, ok, err="": recorded.append((ok, err)))
    monkeypatch.setattr("ui.cards.InfoBar.warning", staticmethod(lambda *a, **k: None))

    task = {"kind": "install", "name": "demo", "meta": _meta("demo")}
    card._active_tasks["demo"] = task
    card._task_active = True

    from ui.cards import _CANCELLED_MARK

    card._on_task_error(task, f"{_CANCELLED_MARK}已取消")
    _pump(0.02)

    assert recorded == [], "取消不是失败，不得写失败记录"
    assert not row._busy, "取消完成后行必须退出 busy"
    assert "demo" not in card._active_tasks
