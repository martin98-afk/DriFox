# -*- coding: utf-8 -*-
"""回归测试：resize 恢复链「按时间预算分批」（#webview-resize 卡顿修复）

背景
----
resize 松手后卡片要从 preview 占位恢复：``set_resize_preview_mode(False)``
内部 ``viewer.show()`` 是恢复阶段唯一大头（实测 6.4ms/张 短消息、
12ms/张 长回复；``update_height`` 的 runJavaScript 只占 ~1ms/12 张）。
旧实现把「视口 ±400px」内的卡片**一次性全量**恢复，视口内 10+ 张时单帧
冻结 70~130ms；离屏段固定 20 张/批，单帧同样可达百毫秒。

修复：视口内与离屏合并成「视口内优先」的单一队列，交给
``_process_restore_batch`` 按上一批实测耗时自适应批大小（<8ms 扩容、
>20ms 缩容），视口内段批间 16ms、离屏段 30ms。

本文件验证三条语义（不依赖真实 WebEngine）：
1. 视口内卡片不再被一次性恢复，而是进入队列分批推进；
2. 批大小随实测耗时自适应；
3. 滚动即时命中路径 ``_restore_card_now`` 能把卡片从队列摘除并就地恢复。

运行::

    python -m pytest tests/widgets/test_restore_batch_adaptive.py -v
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QRect  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

from app.main_widget import (  # noqa: E402
    OpenAIChatToolWindow,
    _RESTORE_BATCH_INIT,
    _RESTORE_BATCH_MAX,
    _RESTORE_BATCH_MIN,
    _RESTORE_BATCH_SLOW_MS,
)


class _StubCard(QWidget):
    """只实现恢复链用到的最小卡片语义。"""

    def __init__(self, y: int, height: int = 150):
        super().__init__()
        self.setGeometry(0, y, 800, height)
        self._resize_preview_mode = True
        self.preview_calls: list = []

    def set_resize_preview_mode(self, enabled: bool):
        self.preview_calls.append(enabled)
        self._resize_preview_mode = enabled


def _make_window(cards, viewport_h: int = 900):
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._is_destroyed = False
    win._restore_epoch = 0
    win._restore_queue = []
    win._restore_batch_idx = 0
    win._restore_batch_size = _RESTORE_BATCH_INIT
    win._restore_visible_count = 0
    win._end_resize_cycle = MagicMock()

    scroll_area = MagicMock()
    scroll_area.viewport.return_value.rect.return_value = QRect(0, 0, 800, viewport_h)
    scroll_area.viewport.return_value.width.return_value = 800
    scroll_area.verticalScrollBar.return_value.value.return_value = 0
    win.chat_scroll_area = scroll_area

    layout = MagicMock()
    layout.count.return_value = len(cards)
    items = [MagicMock(widget=lambda c=c: c) for c in cards]
    layout.itemAt.side_effect = lambda i: items[i] if 0 <= i < len(items) else None
    win.chat_layout = layout
    return win


@pytest.fixture(autouse=True)
def _orchestrator_current(monkeypatch):
    """让 ResizeOrchestrator 恒定判定「当前页」，避免跨测试污染。"""
    orch = MagicMock()
    orch.is_current.return_value = True
    monkeypatch.setattr("app.main_widget.ResizeOrchestrator.get_instance", lambda: orch)
    return orch


def test_visible_cards_go_through_queue_not_restored_at_once(qapp, monkeypatch):
    """视口内卡片必须进队列分批恢复，不能在 _begin_restore_chain 里一次性恢复。"""
    from PyQt5.QtCore import QTimer

    cards = [_StubCard(i * 150) for i in range(12)]
    win = _make_window(cards)
    scheduled = []
    # 链路用 isinstance(item.widget(), MessageCard) 过滤，桩卡片替换该判据
    monkeypatch.setattr("app.main_widget.MessageCard", _StubCard)
    monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda msec, cb: scheduled.append((msec, cb))))

    win._begin_restore_chain(0)

    # 全部 12 张都进队列（视口 900 + 上下 400 缓冲 → 9 张视口内）
    assert len(win._restore_queue) == 12
    assert win._restore_visible_count == 9
    # 首批只恢复 _RESTORE_BATCH_INIT 张，其余仍在 preview 占位
    restored = [c for c in cards if c.preview_calls]
    assert len(restored) == min(_RESTORE_BATCH_INIT, len(cards))
    assert all(c.preview_calls == [False] for c in restored)
    # 未恢复的卡片保持占位态（等后续批次）
    assert all(c._resize_preview_mode for c in cards[len(restored) :])


def test_batch_size_shrinks_when_slow(qapp, monkeypatch):
    """实测耗时超过慢阈值 → 下一批缩容（长回复场景不会冻帧）。"""
    from PyQt5.QtCore import QTimer

    cards = [_StubCard(i * 150) for i in range(12)]
    win = _make_window(cards)
    monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda msec, cb: None))
    monkeypatch.setattr(
        "app.main_widget.QElapsedTimer",
        lambda: _FakeTimer(_RESTORE_BATCH_SLOW_MS + 10),
    )

    win._restore_queue = cards
    win._restore_batch_idx = 0
    win._restore_batch_size = _RESTORE_BATCH_INIT
    win._process_restore_batch(0)

    assert win._restore_batch_size == max(_RESTORE_BATCH_MIN, _RESTORE_BATCH_INIT - 1)


def test_batch_size_grows_when_fast(qapp, monkeypatch):
    """实测耗时低于快阈值 → 下一批扩容（短消息场景尽快收尾）。"""
    from PyQt5.QtCore import QTimer

    cards = [_StubCard(i * 150) for i in range(12)]
    win = _make_window(cards)
    monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda msec, cb: None))
    monkeypatch.setattr("app.main_widget.QElapsedTimer", lambda: _FakeTimer(1))

    win._restore_queue = cards
    win._restore_batch_idx = 0
    win._restore_batch_size = _RESTORE_BATCH_INIT
    win._process_restore_batch(0)

    assert win._restore_batch_size == min(_RESTORE_BATCH_MAX, _RESTORE_BATCH_INIT + 1)


def test_restore_card_now_removes_from_queue(qapp):
    """滚动即时命中：卡片从队列摘除并就地退出 preview，不会被链重复处理。"""
    cards = [_StubCard(i * 150) for i in range(6)]
    win = _make_window(cards)
    win._restore_queue = list(cards)
    win._restore_batch_idx = 0

    target = cards[3]
    win._restore_card_now(target)

    assert target not in win._restore_queue
    assert target.preview_calls == [False]
    assert target._resize_preview_mode is False
    assert len(win._restore_queue) == 5
    # 幂等：重复调用不应抛错，也不应把队列再缩短
    win._restore_card_now(target)
    assert len(win._restore_queue) == 5


def test_restore_card_now_tolerates_card_outside_queue(qapp):
    """卡片不在队列里（链已收尾/已被摘除）时只恢复、不报错。"""
    card = _StubCard(0)
    win = _make_window([card])
    win._restore_queue = []

    win._restore_card_now(card)

    assert card.preview_calls == [False]
    assert win._restore_queue == []


class _FakeTimer:
    """固定耗时的 QElapsedTimer 替身。"""

    def __init__(self, ms: int):
        self._ms = ms

    def start(self):
        return None

    def elapsed(self):
        return self._ms
