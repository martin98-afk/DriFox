# -*- coding: utf-8 -*-
"""回归测试：resize 恢复链「宽度同步分帧批处理」（侧边栏动画收尾卡顿修复）

背景
----
窗口 resize / 侧边栏动画结束后，``_sync_all_cards_width`` 一次性全量同步
所有卡片宽度，实测 30 张卡冻结主线程 44~132ms（约 1.4ms/张），
表现为「动画放完后界面顿一下」。
修复：拆为 分帧宽度同步（``_process_sync_width_batch``，每批
``_SYNC_WIDTH_BATCH`` 张、批间 singleShot(0) 让出主线程）→
``_begin_restore_chain``（原第二阶段恢复链，epoch 令牌约束不变）。

本文件验证分帧链的调度行为与竞态语义（不依赖真实 WebEngine）。
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication  # noqa: F401

from app.main_widget import OpenAIChatToolWindow, _SYNC_WIDTH_BATCH
from app.widgets.message_card import MessageCard


def _make_window(n_cards: int) -> tuple:
    """构造 _sync_all_cards_width 可运行的最小实例 + n 张 user 卡。"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    # ⚠️ PyQt 对象未经 __init__ 时 getattr 会抛 RuntimeError，必须显式预置
    win._is_destroyed = False
    win._last_chat_viewport_width = 0
    win._restore_epoch = 5
    win._sync_width_queue = []
    win._sync_width_idx = 0
    win._ensure_height_batch = MagicMock(return_value=None)
    win._begin_restore_chain = MagicMock()

    scroll_area = MagicMock()
    scroll_area.viewport.return_value.width.return_value = 800
    win.chat_scroll_area = scroll_area

    cards = [MessageCard(role="user") for _ in range(n_cards)]
    layout = MagicMock()
    layout.count.return_value = n_cards
    items = []
    for c in cards:
        item = MagicMock()
        item.widget.return_value = c
        items.append(item)
    layout.itemAt.side_effect = lambda i: items[i] if 0 <= i < n_cards else None
    win.chat_layout = layout
    return win, cards


def _capture_singleshoot(monkeypatch):
    """捕获 QTimer.singleShot 调用，返回 (records, trigger_all)。"""
    records: list[tuple[int, object]] = []

    def fake_single_shot(msec, callback):
        records.append((msec, callback))

    monkeypatch.setattr(QTimer, "singleShot", staticmethod(fake_single_shot))
    return records


def test_sync_all_dispatches_in_batches(qapp, monkeypatch):
    """宽度同步必须分帧推进：首批 ≤ BATCH，未完成时经 singleShot 续批。"""
    n = _SYNC_WIDTH_BATCH * 2 + 3  # 3 批能收敛
    win, cards = _make_window(n)
    records = _capture_singleshoot(monkeypatch)

    win._sync_all_cards_width()

    # epoch 已递增，恢复链尚未开始（宽度同步未完成）
    assert win._restore_epoch == 6
    win._begin_restore_chain.assert_not_called()
    # 首批恰好 BATCH 张
    assert win._sync_width_idx == _SYNC_WIDTH_BATCH
    assert len(win._sync_width_queue) == n
    # 已排入续批任务
    assert len(records) == 1

    # 推进全部续批 → 队列清空（完成态 idx/queue 复位）→ 进入恢复链
    while records:
        _, cb = records.pop(0)
        cb()
    assert win._sync_width_idx == 0
    assert win._sync_width_queue == []
    win._begin_restore_chain.assert_called_once_with(6)


def test_batch_size_bounded(qapp, monkeypatch):
    """每批同步的卡片数不得超过 _SYNC_WIDTH_BATCH。"""
    n = _SYNC_WIDTH_BATCH + 5
    win, cards = _make_window(n)
    records = _capture_singleshoot(monkeypatch)

    win._sync_all_cards_width()
    synced = win._sync_width_idx
    assert synced == _SYNC_WIDTH_BATCH


def test_stale_epoch_aborts_batch(qapp, monkeypatch):
    """epoch 失配（新一轮 resize 已启动）时续批必须静默退出。"""
    n = _SYNC_WIDTH_BATCH + 5
    win, cards = _make_window(n)
    records = _capture_singleshoot(monkeypatch)

    win._sync_all_cards_width()
    stale_epoch = win._restore_epoch  # 6
    # 模拟新一轮 resize 周期先行启动
    win._restore_epoch = 7

    while records:
        _, cb = records.pop(0)
        cb()  # 续批 epoch=6 ≠ 当前 7 → 直接 return

    assert win._sync_width_idx == _SYNC_WIDTH_BATCH
    win._begin_restore_chain.assert_not_called()


def test_background_page_pauses_chain(qapp, monkeypatch):
    """后台页不得推进分帧链：挂起并标记 dirty，等激活时补跑。"""
    n = _SYNC_WIDTH_BATCH + 5
    win, cards = _make_window(n)
    records = _capture_singleshoot(monkeypatch)

    orchestrator = MagicMock()
    orchestrator.is_current.return_value = False
    with patch("app.main_widget.ResizeOrchestrator.get_instance", return_value=orchestrator):
        win._sync_all_cards_width()
        assert len(records) == 0, "后台页不应排入任何续批任务"
        orchestrator.mark_paused.assert_called_once_with(win)

    # 激活后（is_current=True）重跑入口即可恢复整条链
    orchestrator.is_current.return_value = True
    with patch("app.main_widget.ResizeOrchestrator.get_instance", return_value=orchestrator):
        win._sync_all_cards_width()
    assert len(records) == 1


def test_height_batch_opens_before_sync(qapp, monkeypatch):
    """高度批量提交必须在宽度同步开始前开启（收敛分帧期间的高度回传）。"""
    win, cards = _make_window(3)
    batch = MagicMock()
    win._ensure_height_batch = MagicMock(return_value=batch)
    _capture_singleshoot(monkeypatch)

    win._sync_all_cards_width()
    batch.begin.assert_called_once()
