# -*- coding: utf-8 -*-
"""回归测试：切换 tab 回来重渲染后，任务行的「下载中」状态不得丢失

复现背景：安装/更新任务进行中（行 busy「下载中…」），切换 tab 后再切回
→ CardManager.show_card → MarketplaceCard.show_card → _schedule_render +
_async_refresh → _render_plugins 全量重建列表 → 行对象销毁重建（_busy 丢失）
→ 「下载中」状态消失、按钮恢复可点（可重复提交同一插件）。

修复：任务持久化在卡片（_task_queue 排队 + _active_task 运行中），行重建
后从任务列表恢复 busy（_restore_task_busy）；已完成任务不在列表中不会误恢复。
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


def _pump(seconds=0.2):
    app = __import__("PySide6.QtWidgets", fromlist=["QApplication"]).QApplication.instance()
    deadline = time.time() + seconds
    while time.time() < deadline:
        if app is not None:
            app.processEvents()
        time.sleep(0.01)


def _make_plugins(n, market="m"):
    return [
        {
            "name": f"plugin-{market}-{i:03d}",
            "version": "1.2.3",
            "description": "这是一个非常长的插件描述文本用于测试换行行为，" + "额外填充内容" * 6,
            "categories": ["工具"],
        }
        for i in range(n)
    ]


class FakeRow:
    """轻量行桩：模拟 _PluginRow 的 busy 状态机"""

    _busy: bool
    _busy_text: str

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
        self._busy = False

    def setVisible(self, visible):
        pass

    def show(self):
        pass

    def update_search_highlight(self, query):
        pass


def test_restore_only_active_and_queued():
    """行重建后：只恢复运行中/排队中任务的行 busy，已完成任务的行不恢复"""
    from ui.cards import MarketplaceCard

    card = MarketplaceCard()
    rows = {f"p{i}": FakeRow(f"p{i}") for i in range(4)}
    card._row_map = rows  # 模拟重建后的新行（全部非 busy）

    # p0 运行中（_active_task），p1/p2 排队中（_task_queue），p3 已完成（不在任何列表）
    card._active_task = {
        "kind": "install",
        "name": "p0",
        "busy_text": "下载中…",
        "status_text": "下载中…",
        "status_color": "gray",
    }
    card._task_queue = [
        {
            "kind": "install",
            "name": "p1",
            "busy_text": "安装中…",
            "status_text": "安装中…",
            "status_color": "gray",
        },
        {
            "kind": "update",
            "name": "p2",
            "busy_text": "更新中…",
            "status_text": "更新中…",
            "status_color": "gray",
        },
    ]

    card._restore_task_busy()

    assert rows["p0"]._busy and rows["p0"]._busy_text == "下载中…", "运行中任务行未恢复 busy"
    assert rows["p1"]._busy and rows["p1"]._busy_text == "安装中…", "排队任务行未恢复 busy"
    assert rows["p2"]._busy and rows["p2"]._busy_text == "更新中…", "排队任务行未恢复 busy"
    assert not rows["p3"]._busy, "已完成任务的行被误恢复 busy"

    card._cleanup_worker()


def test_restore_after_late_row_creation():
    """分批加载场景：重建后行尚未渲染（_row_map 无行）→ 补行后 busy 恢复

    全量重建时行分批渲染（_render_next_batch 每批 _RENDER_BATCH 个）：
    任务行可能在首批之外，_restore_task_busy 时无行可恢复（no-op）；
    后续「加载更多」补行 → _render_rows 再次调用 _restore_task_busy →
    该行出现后必须恢复 busy。
    """
    from ui.cards import MarketplaceCard

    card = MarketplaceCard()
    card._row_map = {}  # 模拟重建后首批渲染完成，任务行尚未创建
    card._active_task = {
        "kind": "install",
        "name": "p-late",
        "busy_text": "下载中…",
        "status_text": "下载中…",
        "status_color": "gray",
    }

    # 行未渲染：no-op，不抛异常
    card._restore_task_busy()

    # 后续补行创建后：恢复 busy
    row = FakeRow("p-late")
    card._row_map["p-late"] = row
    card._restore_task_busy()
    assert row._busy and row._busy_text == "下载中…", "补行后未恢复任务行 busy"

    # 缺 name 的任务：跳过不抛异常
    card._task_queue.append({"kind": "install", "name": "", "busy_text": "安装中…"})
    card._restore_task_busy()  # 不应抛 KeyError

    card._cleanup_worker()


def test_rebuild_preserves_task_busy(monkeypatch):
    """真实渲染路径：提交任务后全量重建（切 tab 回来）→ 下载中状态保持

    端到端：show_card 渲染 → 提交安装任务（真实队列+worker 线程）→
    _render_plugins 全量重建（行对象全部销毁重建）→ 新行必须恢复 busy；
    任务完成后 busy 正常解除。
    """
    from ui.cards import MarketplaceCard
    from ui.installer import PluginInstaller
    from ui.marketplace_manager import MarketplaceSourceManager

    monkeypatch.setattr(
        MarketplaceSourceManager,
        "get_sources",
        lambda self: [{"name": "fake", "source": {"source": "url", "url": "x"}}],
    )
    monkeypatch.setattr(
        MarketplaceSourceManager,
        "fetch_marketplace",
        lambda self, src, force=False: {"name": "fake", "plugins": _make_plugins(30)},
    )
    monkeypatch.setattr(PluginInstaller, "install", lambda self, meta: True)
    monkeypatch.setattr(PluginInstaller, "get_installed_map", lambda self, use_cache=True: {})
    monkeypatch.setattr(PluginInstaller, "get_status_map", lambda self, use_cache=True: {})

    card = MarketplaceCard()
    card.show()
    card.show_card()

    assert _wait_until(lambda: bool(card._row_map)), "首屏未渲染"
    _pump(0.3)

    name = next(iter(card._row_map))
    # Event 控制任务完成时机：断言「重建后 busy 保持」期间任务必须仍在跑
    # （sleep 定长会引入竞态：缩短后可能在 _pump 期间完成导致 busy 被解除）
    import threading

    task_done_evt = threading.Event()
    card._submit_task(
        kind="install",
        name=name,
        fn=lambda: (task_done_evt.wait(5), True)[1],
        busy_text="下载中…",
        status_text="下载中…",
        status_color="gray",
    )
    assert card._row_map[name]._busy, "提交任务后行未置 busy"

    # 模拟切 tab 回来：show_card → 全量重建列表（行对象销毁重建）
    card._render_plugins(list(card._all_plugins))
    _pump(0.3)

    assert card._row_map[name]._busy, "重建后「下载中」状态被刷新没（bug 回归）"
    assert card._row_map[name]._busy_text == "下载中…", "重建后 busy 文案丢失"

    # 放行任务完成 → busy 解除（不误恢复）
    task_done_evt.set()
    assert _wait_until(lambda: not card._task_active), "任务未完成"
    _pump(0.3)
    assert not card._row_map[name]._busy, "任务完成后行仍 busy（误恢复）"

    card._cleanup_worker()
