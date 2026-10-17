# -*- coding: utf-8 -*-
"""回归：流式态自愈闸门 —— 内容长时间不增长时卡片必须自行收尾

缺陷（2026-10-18 真机实证，ui.log / all.log）：
```
01:01:52  worker finished
01:01:53  render-diag md=5396            ← 最后一次渲染，本轮内容到此为止
01:01:56 ↓ dock-blank 此后每 2s 刷一次，viewer 在 1796↔1805 反复横跳
01:02:16  用户手动删除该轮，抖动才停止
```

卡片结束态唯一入口是 `main_widget._on_stream_finished` →
`_current_assistant_card.finish_streaming()`。该单点漏掉时（引用被切走 /
stream_finished 被 UIEngine 的 is_streaming 守卫吞掉 / 卡片已 sip 删除），
卡片**永远停在 _streaming=True**：
  - 坞态不归位、工具区不折叠（用户感知「对话结束卡片状态没变化」）；
  - 高度追踪 tick 常转 → viewer 高度持续振荡、内容反复重排换行。

修复：`MessageCard._guard_stream_end()` 借流式动画定时器做卡片级自愈
（20fps 定时器流式期间本就常转，零额外成本）。

本用例用 stub viewer，不依赖 WebEngine。
"""

import time
from unittest.mock import MagicMock

from app.widgets.message_card import MessageCard  # noqa: E402


def _make_streaming_card(md_len: int = 100, active_tools: bool = False):
    card = MessageCard.__new__(MessageCard)
    card.role = "assistant"
    card._streaming = True
    viewer = MagicMock()
    viewer._markdown_text = "x" * md_len
    card.viewer = viewer
    card._has_active_tools = lambda: active_tools
    card._stream_guard_len = md_len
    card._stream_guard_ts = time.monotonic()
    card.stop_streaming_anim = MagicMock()
    card.finish_streaming = MagicMock()
    # _guard_stream_end 借用 _update_anim 的定时器节拍；本用例直接调用闸门，
    # 但保留 _anim_clock 桩以免牵连 _update_anim 的其余分支。
    card._anim_clock = MagicMock()
    card._anim_clock.restart.return_value = 0.0
    return card, viewer


# ─── 1. 内容停止增长超过阈值 → 必须自愈收尾 ────────────────────


def test_stale_stream_self_finalizes():
    """内容不再增长且逾越阈值 → 自行 finish_streaming（force_dock_off）"""
    card, _v = _make_streaming_card()
    card._stream_guard_ts = time.monotonic() - (card._STREAM_END_GUARD_S + 1.0)
    card._guard_stream_end()
    card.stop_streaming_anim.assert_called_once()
    card.finish_streaming.assert_called_once_with(force_dock_off=True)


# ─── 2. 正常流式不得误触发 ──────────────────────────────────────


def test_growing_content_never_self_finalizes():
    """内容还在增长 → 只刷新基准，绝不收尾（误触发会打断正常对话）"""
    card, viewer = _make_streaming_card(md_len=100)
    card._stream_guard_ts = time.monotonic() - 999.0  # 即便基准极旧
    viewer._markdown_text = "x" * 500  # 内容变了
    card._guard_stream_end()
    card.finish_streaming.assert_not_called()
    assert card._stream_guard_len == 500  # 基准已刷新


def test_before_threshold_never_self_finalizes():
    """未逾越阈值 → 不收尾（模型短暂静默是正常态）"""
    card, _v = _make_streaming_card()
    card._stream_guard_ts = time.monotonic() - 1.0
    card._guard_stream_end()
    card.finish_streaming.assert_not_called()


def test_active_tools_suppress_guard():
    """仍有工具在执行 → 不收尾（模型在等工具结果，几十秒无文本是正常态）"""
    card, _v = _make_streaming_card(active_tools=True)
    card._stream_guard_ts = time.monotonic() - 999.0
    card._guard_stream_end()
    card.finish_streaming.assert_not_called()


# ─── 3. 未武装不得参与（历史卡 / 已收尾卡）────────────────────


def test_unarmed_guard_is_inert():
    """_stream_guard_len < 0（未武装）→ 闸门惰性，绝不收尾"""
    card, _v = _make_streaming_card()
    card._stream_guard_len = -1
    card._stream_guard_ts = time.monotonic() - 999.0
    card._guard_stream_end()
    card.finish_streaming.assert_not_called()


# ─── 4. 懒武装：待取样态必须记录基准，不得永久惰性 ────────────────


def test_pending_arms_on_first_run():
    """_stream_guard_len=-1（新一轮待取样）首次运行必须记录基准

    🐛 回归：原实现见到 -1 直接 return 且从不赋值 → 恒为 -1 → 闸门永
    远判不到增长，自愈从不触发（真机日志零条 stream-guard）。
    """
    card, _v = _make_streaming_card(md_len=100)
    card._stream_guard_len = -1
    card._guard_stream_end()
    assert card._stream_guard_len == 100, "首次运行必须懒武装，记录当前内容长度为基准"
    card.finish_streaming.assert_not_called()  # 本拍只取样，不判定
    # 第二拍：内容未增长且逾越阈值 → 必须收尾（证明武装后闸门真的能跑）
    card._stream_guard_ts = time.monotonic() - (card._STREAM_END_GUARD_S + 1.0)
    card._guard_stream_end()
    card.finish_streaming.assert_called_once_with(force_dock_off=True)
