# -*- coding: utf-8 -*-
"""MessageCard.cleanup() 必须释放流式文本缓冲 `_stream_text_acc`。

背景：``update_content``（message_card.py:3620）在流式期间**逐 chunk 累加原文**
到 ``_stream_text_acc``，用作 footer 插件的 ``live_text`` 数据源（:1128）。
轮次落定时有两处清零（``set_meta_info`` elapsed 分支 :963 /
``finish_streaming`` :5539），但 ``cleanup()`` 的清空清单（:5627-5653）**漏了
这一项** —— 卡片被回收/销毁时若还没走到落定清零，这张卡就永久持有整轮流式
原文的副本（实测约 20 KB/卡，长会话单调增长）。

修复：在 ``cleanup()`` 的清空清单里补 ``self._stream_text_acc = None``，
与其他大文本字段（``_markdown_text`` / ``_last_rendered_html``）风格一致。
读取点 :1128 用 ``getattr(..., "") or ""`` 兜底，置 None 不会出错。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import app.widgets.message_card as mc  # noqa: E402


def _make_streamed_card(qapp, monkeypatch, chunks: int = 5, size: int = 200):
    """构造一张「流式过但未落定」的 assistant 卡片：缓冲里有实质内容。"""
    monkeypatch.setattr(mc.time, "time", lambda: 1000.0)
    card = mc.MessageCard(role="assistant")
    card.append_text = lambda *a, **k: None
    card.start_elapsed_tracking()
    for _ in range(chunks):
        card.update_content("中" * size)
    return card


def test_cleanup_releases_stream_text_acc(qapp, monkeypatch):
    """cleanup() 后 `_stream_text_acc` 必须不再持有流式原文副本。"""
    card = _make_streamed_card(qapp, monkeypatch)
    before = card._stream_text_acc
    assert len(before) == 1000, f"前置条件不成立：缓冲未累积到预期长度（{len(before)}）"

    card.cleanup()

    # 置 None 或空串都算释放（不锁定具体实现，只锁定「不再持有内容」）
    after = getattr(card, "_stream_text_acc", None)
    assert not after, f"cleanup() 未释放 _stream_text_acc，仍残留 {len(after or '')} 字符"


def test_cleanup_safe_when_never_streamed(qapp):
    """从未流式的卡片（无该字段）调 cleanup() 不得抛错。"""
    card = mc.MessageCard(role="assistant")
    card.append_text = lambda *a, **k: None
    card.cleanup()  # 不抛异常即通过


def test_live_text_read_after_cleanup_is_safe(qapp, monkeypatch):
    """cleanup() 后再取 footer 上下文：live_text 回落为空串，不抛错。

    读取点 :1128 用 ``getattr(...) or ""`` 兜底，置 None 后仍应安全。
    """
    card = _make_streamed_card(qapp, monkeypatch)
    card.cleanup()
    ctx = card._footer_stat_context(streaming=True)
    assert ctx["live_text"] == ""
