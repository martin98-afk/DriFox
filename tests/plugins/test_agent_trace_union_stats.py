# -*- coding: utf-8 -*-
"""agent_trace 统计栏并集口径回归：并发/拆条时长不得重复累计。

背景（2026-09-03 真实 bug）：宿主 chat_worker 落盘时把一次 API 响应里的
**每个并行 tool_call 拆成一条独立 assistant 消息**（``_build_response_message_sequence``
对每个 ``tool_call_marker`` 逐个建条），N 条消息共享同一次 API 调用的
``elapsed_ms``、同一 ``ts_ms`` 写入 → 投影出的 N 条 ASSISTANT 记录区间
**完全重叠**。旧统计 ``Σ duration_ms`` 把同一次调用计时 N 遍，LLM 总时长
放大 2-4 倍（实测会话 883302d6 / cdcbf46e / 139c8f25）。

修复：``trace_models.merge_overlapping`` 按区间并集（墙钟）统计，
段数兼作「真实 LLM 调用次数」（同批拆条完全重叠 → 1 段）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtGui")

_ROOT = Path(__file__).resolve().parents[2]
_UI_DIR = _ROOT / "plugins" / "agent_trace" / "ui"
for p in (str(_ROOT), str(_UI_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from trace_models import TraceRecord, merge_overlapping  # noqa: E402


def _rec(kind, start: float, end: float, pending: bool = False) -> TraceRecord:
    return TraceRecord(
        kind=kind,
        label="x",
        preview="",
        raw="",
        start_ts=start,
        end_ts=end,
        is_pending=pending,
    )


def test_empty_returns_zero():
    assert merge_overlapping([]) == (0, 0)


def test_single_span():
    r = [_rec("A", 100.0, 103.5)]
    ms, seg = merge_overlapping(r)
    assert ms == 3500
    assert seg == 1


def test_duplicate_overlapping_spans_counted_once():
    """同批拆条：N 条 ASSISTANT 完全同区间 → 并集 = 单条时长，段数 = 1。

    复现 cdcbf46e：一次 API 调用（13.75s）拆成 bash/grep 两条 assistant，
    旧求和口径给出 27.5s。
    """
    t0 = 1000.0
    r = [
        _rec("A", t0, t0 + 13.75),
        _rec("A", t0, t0 + 13.75),
        _rec("A", t0, t0 + 13.75),
    ]
    ms, seg = merge_overlapping(r)
    assert ms == 13750
    assert seg == 1


def test_serial_calls_kept_separate():
    """串行多次调用：不重叠 → 并集 = 各段之和，段数 = 次数。"""
    r = [
        _rec("A", 100.0, 102.0),
        _rec("A", 103.0, 105.0),
        _rec("A", 106.0, 108.5),
    ]
    ms, seg = merge_overlapping(r)
    assert ms == 6500
    assert seg == 3


def test_partial_overlap_merged():
    r = [
        _rec("A", 100.0, 102.0),
        _rec("A", 101.0, 104.0),
    ]
    ms, seg = merge_overlapping(r)
    assert ms == 4000
    assert seg == 1


def test_adjacent_spans_merge():
    """相接（start == 前段 end）视为连续 —— 毫秒打点相接基本只在同一次调用链内。"""
    r = [
        _rec("A", 100.0, 102.0),
        _rec("A", 102.0, 104.0),
    ]
    ms, seg = merge_overlapping(r)
    assert ms == 4000
    assert seg == 1


def test_instant_records_ignored():
    """无 elapsed_ms 的历史消息（end <= start 且非 pending）不贡献时长。"""
    r = [
        _rec("A", 100.0, 0.0),  # 瞬时
        _rec("A", 100.0, 101.0),
    ]
    ms, seg = merge_overlapping(r)
    assert ms == 1000
    assert seg == 1


def test_invalid_start_ignored():
    r = [_rec("A", 0.0, 5.0), _rec("A", 200.0, 201.0)]
    ms, seg = merge_overlapping(r)
    assert ms == 1000
    assert seg == 1


def test_pending_uses_now():
    now = time.time()
    r = [_rec("A", now - 2.0, 0.0, pending=True)]
    ms, seg = merge_overlapping(r)
    assert 1900 <= ms <= 4000  # 时钟抖动容忍
    assert seg == 1


def test_unsorted_input():
    r = [
        _rec("A", 106.0, 108.0),
        _rec("A", 100.0, 103.0),
        _rec("A", 101.0, 107.0),
    ]
    ms, seg = merge_overlapping(r)
    assert ms == 8000
    assert seg == 1
