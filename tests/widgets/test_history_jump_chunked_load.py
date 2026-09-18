# -*- coding: utf-8 -*-
"""历史问题跳转：分批 prepend 加载 + 摘除前先隐藏（白窗护栏）。

## 用户可见现象

① 点「历史问题」第一条跳转，长会话肉眼可见卡死。
根因：`_load_history_to_index` 把 ``target.._visible_batch_start`` 之间**全部**
批次一次性 prepend 建卡 —— 长会话数百批 × 每批若干卡，每张含 markdown 转换
与 ``insertWidget(0)`` 布局搬移，全部塞进一帧，主线程长阻塞。注释写的是
「分批加载」，实际只有一次全量 + 一个 100ms 续拍（续拍因已到位立即返回）。

② 跳转/批量回收时软件窗口之外浮出一排白色弹窗。
根因：占位 widget 与 ``CodeWebViewer`` 都是**已 show 的可见对象**，
``setParent(None)`` 前没有 ``hide()`` → 脱离父窗口树即变成独立顶层窗口，
要等 ``deleteLater`` 真正执行才消失。项目里其它 detach 点
（``_clear_chat_area`` / ``ui_helpers.delete_widgets_from_layout``）都有该护栏，
唯独这两处漏了。
"""

import sys
from unittest.mock import MagicMock

import pytest
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QVBoxLayout, QWidget

from app.main_widget import OpenAIChatToolWindow
from app.widgets.message_card import MessageCard


def _make_win(batch_count: int = 60, visible_start: int | None = None, step: int = 8):
    """`__new__` 最小桩（同 test_recycle_rendered_guard.py 法）。"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._incremental_visible_batch_count = step
    win._message_batch = [[{"role": "user", "content": f"q{i}"}] for i in range(batch_count)]
    win._visible_batch_start = visible_start if visible_start is not None else batch_count
    win._batch_cards = [None] * batch_count
    win._batch_placeholders = {}
    return win


def _instrument(win, monkeypatch):
    """接管 QTimer.singleShot，记录续拍回调而不真跑事件循环。"""
    fired: list = []
    monkeypatch.setattr(QTimer, "singleShot", lambda ms, cb: fired.append((ms, cb)))
    calls: list = []
    win._render_message_to_card = lambda batches, insert_at_top=False, batch_offset=0: calls.append(
        (batch_offset, len(batches), insert_at_top)
    )
    win._scroll_to_pending_target = lambda: calls.append(("scroll",))
    return calls, fired


def test_load_history_prepends_one_chunk_per_tick(monkeypatch):
    """每拍只 prepend 一个步长（8 批），不一次性吃掉整段历史。"""
    win = _make_win(batch_count=60, visible_start=60)
    calls, fired = _instrument(win, monkeypatch)

    win._load_history_to_index(0)

    assert calls == [(52, 8, True)], "首拍只补 8 批，且插到布局顶部"
    assert win._visible_batch_start == 52
    assert len(fired) == 1 and fired[0][0] == 100, "续拍挂 100ms 定时器"


def test_load_history_drains_to_target_then_scrolls(monkeypatch):
    """续拍逐段推进：60 → 52 → … → 0，到位后走滚动分支，不再建卡。"""
    win = _make_win(batch_count=60, visible_start=60)
    calls, fired = _instrument(win, monkeypatch)

    win._load_history_to_index(0)
    # 手动驱动续拍（不跑事件循环）
    for _ in range(20):
        if not fired:
            break
        _ms, cb = fired.pop(0)
        cb()

    assert win._visible_batch_start == 0
    assert calls[-1] == ("scroll",)
    assert sum(1 for c in calls if c[0] != "scroll") == 8, "60 批 / 每拍 8 批 = 8 拍"
    chunks = [c for c in calls if c[0] != "scroll"]
    assert all(n <= 8 for _off, n, _top in chunks), "任何一拍都不超过步长"


def test_load_history_tail_chunk_is_clamped(monkeypatch):
    """剩余不足一个步长时只取剩余部分，不越界到目标之前。"""
    win = _make_win(batch_count=60, visible_start=60)
    calls, _fired = _instrument(win, monkeypatch)

    win._load_history_to_index(57)

    assert calls == [(57, 3, True)]
    assert win._visible_batch_start == 57


def test_load_history_already_loaded_scrolls_directly(monkeypatch):
    """目标已在加载窗口内时不建卡，直接滚动。"""
    win = _make_win(batch_count=60, visible_start=20)
    calls, fired = _instrument(win, monkeypatch)

    win._load_history_to_index(30)

    assert calls == [("scroll",)]
    assert fired == []


def test_remove_placeholder_hides_before_reparent(qapp):
    """占位摘除必须先 hide()：否则可见 widget 脱离父树变成独立白窗。"""
    win = _make_win()
    container = QWidget()
    layout = QVBoxLayout(container)
    ph = QWidget(container)
    ph.setFixedHeight(120)
    layout.addWidget(ph)
    win.chat_layout = layout

    win._remove_placeholder_widget(ph)

    assert ph.isHidden(), "setParent(None) 之前必须 hide()，避免顶层白窗"
    assert ph.parent() is None
    assert layout.indexOf(ph) < 0


def test_detach_viewer_hides_before_reparent_in_source():
    """源码断言：CodeWebViewer detach 先 hide() 再 setParent(None)。

    构造真实 QWebEngineView 代价高（需 Chromium 初始化），且不可见时行为与
    实机不一致，故锁源码顺序而非行为。
    """
    with open("app/widgets/message_card.py", encoding="utf-8") as f:
        src = f.read()
    start = src.index("def detach_viewer(")
    body = src[start : src.index("self.viewer = None", start)]
    assert "viewer.hide()" in body, "detach 前必须隐藏 viewer"
    assert body.index("viewer.hide()") < body.index("viewer.setParent(None)"), "hide 必须在断父之前"


def _make_scroll_win():
    """跳转定位专用桩：布局为空、滚动条可观测。"""
    win = _make_win(batch_count=6, visible_start=0)
    win._is_virtual_recycling = False
    win._is_widget_alive = lambda w: True
    win._pending_scroll_to_update = None
    win.chat_layout = MagicMock()
    win.chat_layout.count.return_value = 0
    win.chat_layout.itemAt.return_value = None
    sb = MagicMock()
    area = MagicMock()
    area.verticalScrollBar.return_value = sb
    win.chat_scroll_area = area
    return win, sb


def test_restore_unloaded_batch_before_scroll():
    """目标批次只剩占位时先原位重建再定位（点最后一个历史问题的静默失败）。"""
    win, sb = _make_scroll_win()
    card = MessageCard(role="user")
    card._message_index = 5
    win._batch_placeholders[5] = MagicMock()
    win.chat_layout.indexOf.return_value = 4
    rendered = []

    def _render(batches, insert_at_top=False, batch_offset=0, anchor_layout_index=None):
        win._batch_cards[batch_offset] = [card]
        rendered.append((batch_offset, anchor_layout_index))

    win._render_message_to_card = _render
    win._scroll_to_batch_index(5, node_index=3)

    assert rendered == [(5, 4)], "按占位所在布局位原位重建，不得追加到末尾"
    assert sb.setValue.called, "重建后必须定位到目标卡片"
    assert win._pending_scroll_to_update == 3


def test_scroll_skips_restore_when_cards_alive():
    """卡片仍在时直接定位，不做多余重建。"""
    win, sb = _make_scroll_win()
    card = MessageCard(role="user")
    card._message_index = 2
    win._batch_cards[2] = [card]
    win._render_message_to_card = MagicMock()

    win._scroll_to_batch_index(2)

    win._render_message_to_card.assert_not_called()
    assert sb.setValue.called


def test_restore_batch_ui_noop_without_placeholder():
    """无占位 / 已渲染 / 空数据都不重建（占位丢失时保持旧行为，不制造错序卡片）。"""
    win, _sb = _make_scroll_win()
    win._render_message_to_card = MagicMock()

    assert win._restore_batch_ui(0) is False, "无占位不重建"
    win._batch_cards[1] = [MessageCard(role="user")]
    assert win._restore_batch_ui(1) is False, "已渲染批次不重复重建"
    win._message_batch[2] = []
    win._batch_placeholders[2] = MagicMock()
    win.chat_layout.indexOf.return_value = 1
    assert win._restore_batch_ui(2) is False, "空数据批次不重建"
    win._render_message_to_card.assert_not_called()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
