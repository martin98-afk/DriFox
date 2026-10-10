# -*- coding: utf-8 -*-
"""验证：工具调用到达时冲刷残留思考批次（信号层修复）。

根因（chat_worker._process_responses_stream）
--------------------------------------------
`_reasoning_batch` 原先只在三处冲刷：取消时、批内 >=20 字或距上次 >80ms、
**流结束后**。工具调用事件到达时不冲刷 → 模型最后吐的一小段思考（<20 字）
滞留到流结束（= 工具执行完）才发射，用户感知「思考等工具执行完才刷新」。

验证设计
--------
用桩 response 驱动真实 `_process_responses_stream`，事件序列：
  reasoning(22字) → reasoning(8字) → 工具 added/delta/done → [尾部停顿 250ms]
尾部停顿模拟「工具执行耗时」。判据：
  - 最后一批 reasoning 回调若在停顿**之前**（<200ms）发出 → 工具事件已冲刷（修复生效）
  - 若在停顿**之后**（>=200ms）→ 残留至流结束（未修复）

运行：python tests/debug/reasoning_batch_flush_probe.py
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DRIFOX_DATA_DIR", os.path.join(os.path.dirname(__file__), "_probe_data"))

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)


class Ev:
    """极简事件桩（模拟 Responses API 事件对象）。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def build_events():
    """reasoning(22) -> reasoning(8) -> 工具 added/delta/done。"""
    return [
        Ev(type="response.reasoning_summary_text.delta", delta="A" * 22),
        Ev(type="response.reasoning_summary_text.delta", delta="B" * 8),
        Ev(type="response.output_item.added", item={"type": "function_call", "id": "fc_1", "name": "read"}),
        Ev(type="response.function_call_arguments.delta", item_id="fc_1", delta='{"path":'),
        Ev(type="response.function_call_arguments.done", item_id="fc_1", arguments='{"path":"a.py"}'),
    ]


class DelayTailIter:
    """事件耗尽后停顿 250ms 再发哨兵（模拟工具执行耗时）。"""

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


def main():
    from PyQt5.QtCore import QThread

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

    timeline = []
    t_start = time.monotonic()

    def _emit(name, sig, *args):
        timeline.append((round((time.monotonic() - t_start) * 1000), name, str(args)[:50]))

    worker._emit_with_callback = _emit
    worker._responses_item_get = lambda item, key: (item or {}).get(key)
    worker._get_reasoning_content = lambda: "".join(worker._reasoning_chunks)
    worker._guarded_stream_iter = lambda resp: resp

    print("=== 驱动真实 _process_responses_stream ===", flush=True)
    print("  事件: reasoning(22字) -> reasoning(8字) -> 工具 added/delta/done -> [停顿 250ms]", flush=True)

    try:
        worker._process_responses_stream(DelayTailIter(build_events()))
    except Exception as e:  # noqa: BLE001
        print("  流处理异常（桩不完整属正常）:", type(e).__name__, e, flush=True)

    t_end = round((time.monotonic() - t_start) * 1000)
    print("\n=== 时间线 ===", flush=True)
    for ts, name, args in timeline:
        print(f"  +{ts:>5}ms  {name}  {args}", flush=True)
    print(f"  +{t_end:>5}ms  [流结束]", flush=True)

    r_events = [t for t in timeline if t[1] == "reasoning_content_received"]
    print("\n=== 判据 ===", flush=True)
    print(f"  reasoning 回调: {[t[0] for t in r_events]} ms", flush=True)
    if not r_events:
        print("  [X] 未捕获 reasoning 回调（桩问题）", flush=True)
        return
    last_r = r_events[-1][0]
    if last_r >= 200:
        print(f"  [X] 最后一批 reasoning 在【尾部停顿后】才发出（+{last_r}ms）", flush=True)
        print("      -> 未冲刷：残留至流结束", flush=True)
    else:
        print(f"  [OK] 最后一批 reasoning 在【工具事件到达时】已发出（+{last_r}ms，停顿前）", flush=True)
        print("      -> 修复生效：工具调用到达时冲刷了残留批次", flush=True)

    print("\ndone", flush=True)


main()
