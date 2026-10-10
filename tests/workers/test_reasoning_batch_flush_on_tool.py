# -*- coding: utf-8 -*-
"""回归测试：工具调用到达时冲刷残留思考批次。

背景（用户报告）
----------------
「思考内容更新经常在思考内容下面的工具执行完才刷新」。

根因（chat_worker._process_responses_stream）
--------------------------------------------
`_reasoning_batch` 的冲刷条件原先只有三处：
  1. 取消时
  2. 批内累计 >= 20 字 或 距上次发射 > 0.08s
  3. **流结束后**（for 循环外）

工具调用事件（response.function_call_arguments.delta /
response.output_item.added）到达时**不冲刷**。模型最后吐的一小段思考
（<20 字且距上次发射 <80ms）会滞留在批次里，等到流结束（= 工具执行完）
才发射 —— 这正是用户感知的延迟。

修复
----
在上述两个工具事件分支入口补冲刷，让思考在工具调用开始的瞬间落地。

本测试
------
用桩 response 驱动真实 `_process_responses_stream`，测量最后一批 reasoning
的回调时刻：
  - 修复前：在尾部停顿（模拟工具执行）**之后**才发出
  - 修复后：在工具事件到达时（停顿**之前**）已发出

运行：python -m pytest tests/workers/test_reasoning_batch_flush_on_tool.py -v
"""

import os
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QThread  # noqa: E402


class Ev:
    """极简事件桩（模拟 Responses API 事件对象）。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _build_events():
    """reasoning(22字) -> reasoning(8字) -> 工具 added/delta/done。"""
    return [
        Ev(type="response.reasoning_summary_text.delta", delta="A" * 22),
        Ev(type="response.reasoning_summary_text.delta", delta="B" * 8),
        Ev(type="response.output_item.added", item={"type": "function_call", "id": "fc_1", "name": "read"}),
        Ev(type="response.function_call_arguments.delta", item_id="fc_1", delta='{"path":'),
        Ev(type="response.function_call_arguments.done", item_id="fc_1", arguments='{"path":"a.py"}'),
    ]


class _DelayTailIter:
    """事件耗尽后停顿 tail_delay 再发哨兵（模拟工具执行耗时）。"""

    def __init__(self, evs, tail_delay=0.25):
        self._it = iter(evs)
        self._tail_delay = tail_delay
        self._slept = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._it)
        except StopIteration:
            if not self._slept:
                self._slept = True
                time.sleep(self._tail_delay)
                return Ev(type="__tail_sentinel__")
            raise


def _make_worker():
    """构造最小 worker（只具备 _process_responses_stream 所需属性）。"""
    from app.core.workers import chat_worker as cw

    WorkerCls = cw.OpenAIChatWorker
    worker = WorkerCls.__new__(WorkerCls)
    QThread.__init__(worker)
    worker._is_cancelled = False
    worker._response_chunks = []
    worker._response_content_blocks = []
    worker._reasoning_chunks = []
    worker._current_tool_calls = {}
    worker._tool_calls_buffer = {}
    worker._previewed_tool_call_ids = set()
    worker._tool_calls_index_to_id = {}
    worker._chunks_total_len = 0
    worker._last_ttft_ms = 0.0
    worker._llm_req_t0 = time.monotonic()
    worker._current_response = None
    worker._DEFERRED_PREVIEW_TOOLS = getattr(WorkerCls, "_DEFERRED_PREVIEW_TOOLS", frozenset())
    worker.tool_start_callback = None
    worker._responses_item_get = lambda item, key: (item or {}).get(key)
    worker._get_reasoning_content = lambda: "".join(worker._reasoning_chunks)
    worker._guarded_stream_iter = lambda resp: resp
    return worker


def test_reasoning_batch_flushed_when_tool_call_arrives():
    """工具事件到达时，残留思考批次必须立即冲刷（而非等流结束）。"""
    worker = _make_worker()
    timeline = []
    t_start = time.monotonic()

    def _emit(name, sig, *args):
        timeline.append((round((time.monotonic() - t_start) * 1000), name, str(args)[:40]))

    worker._emit_with_callback = _emit

    try:
        worker._process_responses_stream(_DelayTailIter(_build_events()))
    except Exception:  # noqa: BLE001
        # 桩不完整导致的收尾异常与本次断言无关（信号已在流处理中捕获）
        pass

    r_events = [t for t in timeline if t[1] == "reasoning_content_received"]
    assert r_events, "未捕获 reasoning_content_received 回调"

    # 两条 reasoning：22 字（触发批内阈值）+ 8 字（应被工具事件冲刷）
    assert len(r_events) >= 1, f"期望收到 reasoning 回调，实际 {r_events}"
    last_ms = r_events[-1][0]
    # 尾部停顿 250ms 模拟工具执行：修复生效时最后一条在停顿前（<200ms）发出
    assert last_ms < 200, (
        f"最后一批 reasoning 在 {last_ms}ms 才发出（尾部停顿 250ms 之后）——"
        f"说明工具调用到达时未冲刷残留批次，思考会等工具执行完才刷新"
    )


def test_reasoning_content_preserved_after_flush():
    """冲刷不得丢字：全部 reasoning 片段都应送达（拼接后完整）。"""
    worker = _make_worker()
    received = []

    def _emit(name, sig, *args):
        if name == "reasoning_content_received" and args:
            received.append(str(args[0]))

    worker._emit_with_callback = _emit

    try:
        worker._process_responses_stream(_DelayTailIter(_build_events(), tail_delay=0.05))
    except Exception:  # noqa: BLE001
        pass

    joined = "".join(received)
    assert "A" * 22 in joined, f"第一批 22 字思考丢失：{received}"
    assert "B" * 8 in joined, f"第二批 8 字思考丢失：{received}"


def test_flush_branch_present_in_source():
    """源码级守卫：两个工具事件分支都必须含冲刷逻辑（防后续重构误删）。"""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "app" / "core" / "workers" / "chat_worker.py"
    text = src.read_text(encoding="utf-8")
    start = text.find('elif etype == "response.function_call_arguments.delta":')
    assert start != -1
    seg_delta = text[start : start + 900]
    assert "_reasoning_batch" in seg_delta, "function_call_arguments.delta 分支缺冲刷"

    start2 = text.find('elif etype == "response.output_item.added":')
    assert start2 != -1
    seg_added = text[start2 : start2 + 900]
    assert "_reasoning_batch" in seg_added, "output_item.added 分支缺冲刷"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
