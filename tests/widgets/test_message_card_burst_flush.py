# -*- coding: utf-8 -*-
"""批量加载错峰（T11）回归测试。

背景（09-16 崩溃链第一环）：加载 19 条消息的会话时，每张卡片各自
``_schedule_render(immediate=True)`` 在同一帧派发全量重渲，19 份 HTML 解析 +
重排集中到一帧，renderer 进程被压死（0xC0000409 → 全卡自愈 → 进程级死亡）。

修复：历史加载路径把 immediate 渲染降级为合并派发（``immediate_render=False``），
懒渲染队列启动后由 ``_flush_batch_immediate_renders`` 逐卡 80ms 串行补派。

本文件用鸭子类型替身断言参数化透传链与错峰间隔，不拉真实 WebEngine。

运行::

    python -m pytest tests/widgets/test_message_card_burst_flush.py -v
"""

import os
import re
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ─── 默认向后兼容：immediate=True ───────────────────────────────────


def test_viewer_finish_streaming_defaults_to_immediate(qapp):
    """CodeWebViewer.finish_streaming 默认 immediate=True（交互路径行为不变）。"""
    import inspect

    from app.widgets.message_card import CodeWebViewer

    sig = inspect.signature(CodeWebViewer.finish_streaming)
    assert sig.parameters["immediate"].default is True


def test_message_card_finish_streaming_defaults_to_immediate(qapp):
    """MessageCard.finish_streaming 默认 immediate=True 且透传 viewer。"""
    import inspect

    from app.widgets.message_card import MessageCard

    sig = inspect.signature(MessageCard.finish_streaming)
    assert sig.parameters["immediate"].default is True


def test_ui_helpers_render_batch_defaults_to_immediate(qapp):
    """render_batch_to_assistant_card 默认 immediate_render=True（绑定默认值）。"""
    import inspect

    from app.widgets.ui_helpers import render_batch_to_assistant_card

    sig = inspect.signature(render_batch_to_assistant_card)
    assert sig.parameters["immediate_render"].default is True


# ─── immediate=False 跳过 immediate 派发 ───────────────────────────


def test_append_text_skips_immediate_when_disabled(qapp):
    """append_text(immediate_render=False)：自然边界处不派 immediate 全量渲染。"""
    from app.widgets.message_card import MessageCard

    card = MessageCard.__new__(MessageCard)
    viewer = MagicMock()
    viewer._has_reached_clean_boundary.return_value = True
    viewer._think_text_streaming_started = False
    viewer._tag_text_streaming = False
    card.viewer = viewer
    card.role = "assistant"
    card._content_data = [{"type": "text", "text": ""}]
    card._streaming = True
    card._lazy_rendered = True
    card._content_just_loaded = False
    card._height_cache = MagicMock()
    card._pending_content = None

    card.append_text("段落结束。", immediate_render=False)

    # 不得出现 immediate=True 的派发；应为合并派发（immediate=False）
    assert all(call.kwargs.get("immediate") is not True for call in viewer._schedule_render.call_args_list), (
        f"immediate=False 路径仍派发了全量渲染: {viewer._schedule_render.call_args_list}"
    )


def test_append_text_immediate_by_default(qapp):
    """append_text 默认仍在自然边界处派发 immediate 全量渲染（行为不变）。"""
    from app.widgets.message_card import MessageCard

    card = MessageCard.__new__(MessageCard)
    viewer = MagicMock()
    viewer._has_reached_clean_boundary.return_value = True
    viewer._think_text_streaming_started = False
    viewer._tag_text_streaming = False
    card.viewer = viewer
    card.role = "assistant"
    card._content_data = [{"type": "text", "text": ""}]
    card._streaming = True
    card._lazy_rendered = True
    card._content_just_loaded = False
    card._height_cache = MagicMock()
    card._pending_content = None

    card.append_text("段落结束。")

    assert any(call.kwargs.get("immediate") is True for call in viewer._schedule_render.call_args_list), (
        f"默认路径未派发全量渲染: {viewer._schedule_render.call_args_list}"
    )


# ─── 错峰 flush ──────────────────────────────────────────────────────


class _FakeCard:
    """flush 所需的最小卡片接口：viewer / _schedule_render 记录。"""

    def __init__(self):
        self.viewer = MagicMock()
        self.viewer._schedule_render = MagicMock()
        self.rendered_at = []

    def _record(self):
        self.rendered_at.append(time.monotonic())
        self.viewer._schedule_render(immediate=True)


def _flush_window(host, cards, extra_ms=900):
    """驱动 flush（未绑定调用真实方法）并推进事件循环。"""
    from PyQt5.QtWidgets import QApplication

    from app.main_widget import OpenAIChatToolWindow

    OpenAIChatToolWindow._flush_batch_immediate_renders(host, cards)
    deadline = time.monotonic() + extra_ms / 1000.0
    app = QApplication.instance()
    if app is None:
        return
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def test_flush_spreads_19_cards_over_time(qapp):
    """19 卡不得同帧全量重渲：首末派发时刻必须散开（错峰核心断言）。"""
    from app.main_widget import OpenAIChatToolWindow

    host = MagicMock()
    host._is_widget_alive = lambda card: True

    cards = []
    for _ in range(19):
        card = _FakeCard()
        card.viewer._schedule_render = MagicMock(
            side_effect=lambda c=card, **kw: c.rendered_at.append(time.monotonic())
        )
        cards.append(card)

    _flush_window(host, cards, extra_ms=2500)

    assert all(c.viewer._schedule_render.call_count == 1 for c in cards), "每卡应恰好补派一次"
    stamps = sorted(c.rendered_at[0] for c in cards)
    spread_ms = (stamps[-1] - stamps[0]) * 1000.0
    assert spread_ms > 500, f"19 卡渲染时刻过于集中（spread={spread_ms:.0f}ms），错峰未生效"


def test_flush_interval_is_consistent(qapp):
    """相邻卡片派发间隔应贴近 80ms 基线（允许调度抖动）。"""
    from app.main_widget import OpenAIChatToolWindow

    host = MagicMock()
    host._is_widget_alive = lambda card: True

    cards = [_FakeCard() for _ in range(4)]
    for c in cards:
        c.viewer._schedule_render = MagicMock(side_effect=lambda c=c, **kw: c.rendered_at.append(time.monotonic()))

    _flush_window(host, cards, extra_ms=1200)

    stamps = sorted(c.rendered_at[0] for c in cards if c.rendered_at)
    assert len(stamps) == 4, f"应全部补派，实际 {len(stamps)}"
    gaps = [(b - a) * 1000.0 for a, b in zip(stamps, stamps[1:])]
    assert all(40 <= g <= 400 for g in gaps), f"间隔偏离 80ms 基线过多: {gaps}"


def test_flush_skips_dead_cards(qapp):
    """已销毁卡（_is_widget_alive False）跳过，不影响其余卡片补派。"""
    from app.main_widget import OpenAIChatToolWindow

    host = MagicMock()
    dead = _FakeCard()
    alive = _FakeCard()
    alive.viewer._schedule_render = MagicMock(side_effect=lambda **kw: alive.rendered_at.append(time.monotonic()))
    host._is_widget_alive = lambda card: card is not dead

    _flush_window(host, [dead, alive], extra_ms=600)

    dead.viewer._schedule_render.assert_not_called()
    assert alive.viewer._schedule_render.call_count == 1


def test_flush_empty_list_is_noop(qapp):
    """空列表 / None 不抛异常（加载路径可能无待补卡片）。"""
    from app.main_widget import OpenAIChatToolWindow

    host = MagicMock()
    host._is_widget_alive = lambda card: True

    OpenAIChatToolWindow._flush_batch_immediate_renders(host, [])
    OpenAIChatToolWindow._flush_batch_immediate_renders(host, None)
    qapp.processEvents()


def test_flush_survives_sip_deleted_viewer(qapp):
    """viewer 已销毁（sip 删除 → RuntimeError）时单卡失败不中断整批。"""
    from app.main_widget import OpenAIChatToolWindow

    host = MagicMock()
    host._is_widget_alive = lambda card: True

    broken = _FakeCard()
    broken.viewer._schedule_render = MagicMock(side_effect=RuntimeError("wrapped C/C++ object deleted"))
    ok = _FakeCard()
    ok.viewer._schedule_render = MagicMock(side_effect=lambda **kw: ok.rendered_at.append(time.monotonic()))

    _flush_window(host, [broken, ok], extra_ms=600)

    assert ok.viewer._schedule_render.call_count == 1, "前卡异常导致后续卡片未被补派"


def test_history_load_path_passes_immediate_false():
    """历史加载调用点必须传 immediate_render=False（源码锚点断言）。"""
    src = (PROJECT_ROOT / "app" / "main_widget.py").read_text(encoding="utf-8")
    assert re.search(
        r"render_batch_to_assistant_card\(\s*assistant_card,\s*batch,\s*immediate_render=False\s*\)",
        src,
    ), "历史加载路径未降级 immediate 渲染"
    assert "_flush_batch_immediate_renders(pending_lazy_cards)" in src, "缺少错峰补渲调度"
