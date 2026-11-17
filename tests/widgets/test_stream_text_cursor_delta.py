# -*- coding: utf-8 -*-
"""[P2-1] _stream_text_acc 增量游标行为锁定。

背景：流式页脚统计累计串的目标形态——写入侧（update_content 每批）只
append 片段到 _stream_text_parts（O(1)），读取点（footer 200ms 采样拍）
才固化拼接，live_text_delta 只含「上次采样以来的新增片段」（T4-5）。

锁定三个行为，防回归为每批 ``acc += txt`` 的 O(n²) 旧实现（旧坑：读取方
持有引用使 CPython 原地 realloc 失效，实测 390KB 回复 516ms 全落主线程）：
1. 游标只吐新增：第二次采样 delta 仅含两批之间新增部分
2. live_text 整串语义不变（provider 兼容契约，见 ui_plugin_registry 文档）
3. 清理点：_stream_text_clear 后本拍 delta 退化全量并复位游标；
   cleanup 后 parts/acc 释放（与 test_message_card_cleanup_stream_text_acc 同源）
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


def _make_card():
    """assistant 卡 + 流式计时启动（_stream_text_parts 在此初始化）。"""
    card = mc.MessageCard(role="assistant")
    card.start_elapsed_tracking()
    return card


def test_cursor_only_emits_new_delta(qapp):
    """第二次采样 delta 只含新增片段，live_text 保持整串语义。"""
    card = _make_card()
    card.update_content("AAAA")
    ctx1 = card._footer_stat_context(streaming=True)
    assert ctx1["live_text"] == "AAAA"
    assert ctx1["live_text_delta"] == "AAAA"
    assert card._stream_text_cursor == 4

    card.update_content("BBBB")
    ctx2 = card._footer_stat_context(streaming=True)
    assert ctx2["live_text"] == "AAAABBBB"      # 整串契约不变
    assert ctx2["live_text_delta"] == "BBBB"    # 游标只吐新增，不含已采样部分
    assert card._stream_text_cursor == 8


def test_clear_resets_cursor_to_full_delta(qapp):
    """累计串被清空（新一轮/清空）后，本拍 delta 退化全量并复位游标。"""
    card = _make_card()
    card.update_content("AAAA")
    card._footer_stat_context(streaming=True)  # cursor = 4
    card._stream_text_clear()
    card.update_content("XY")
    ctx = card._footer_stat_context(streaming=True)
    assert ctx["live_text"] == "XY"
    # cursor(4) > len("XY") → 累计串被重置，delta 退化全量
    assert ctx["live_text_delta"] == "XY"
    assert card._stream_text_cursor == 2


def test_cleanup_releases_stream_buffers(qapp):
    """cleanup 后 parts/acc 置 None（释放契约；None 分支由 snapshot 兜底空串）。"""
    card = _make_card()
    card.update_content("AAAA")
    card._footer_stat_context(streaming=True)
    card.cleanup()
    assert card._stream_text_parts is None
    assert card._stream_text_acc is None
    # 释放后读取安全（snapshot 的 parts is None 兜底分支）
    assert card._footer_stat_context(streaming=True)["live_text"] == ""
