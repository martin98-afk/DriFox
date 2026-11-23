# -*- coding: utf-8 -*-
"""openai_chat sink content 批处理阈值回归（T2-B：30→120 字）。

背景（2026-11-18 T-A 诊断）：3 万字符流式 14s 中喂入节拍占 89%
（批数 × Windows 时钟下限 15.6ms/拍），批体积 30→120 字使批数降 4 倍。

保护面：
1. 分批边界语义：恰 119/120/121 字的发射行为（阈值先到者发，80ms 独立保底）
2. 批聚合完整有序：多批拼接 == 输入全文（含 markdown 围栏跨批，切点任意
   时下游 UI append_chunk 增量注入本就容忍）
3. 80ms 保底发射与字数阈值解耦：慢流（<120 字/80ms）不滞留
"""

import importlib.util
import threading
import time
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parent.parent.parent / "plugins" / "system-stream-sinks"
_MODULE_PATH = _PLUGIN / "stream_sinks" / "openai_chat.py"


def _load():
    spec = importlib.util.spec_from_file_location("openai_chat_sink_batch_test", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Tracker:
    def record_usage(self, usage):
        pass


class _Ctx:
    """最小状态口（属性名对齐 SinkContext 契约，同 test_stream_sink_ttft.py）。"""

    DEFERRED_PREVIEW_TOOLS = set()

    def __init__(self):
        self.stream_lock = threading.Lock()
        self.response = None
        self.current_response = None
        self.stream = True
        self.response_content_blocks = []
        self.current_tool_calls = {}
        self.tool_calls_buffer = {}
        self.tool_calls_index_to_id = {}
        self.response_chunks = []
        self.chunks_total_len = 0
        self.reasoning_content = ""
        self.reasoning_chunks = []
        self.last_usage = None
        self.cache_tracker = _Tracker()
        self.waiting_tool_params = {}
        self.previewed_tool_call_ids = set()
        self.last_progress_len = {}
        self.last_progress_ts = {}
        self.last_est_len = {}
        self.last_line_est = {}
        self.is_cancelled = False
        self.streaming_rss_base = 0.0
        self.mem_total_chunks_logged = 0
        self.last_ttft_ms = 0.0
        self.llm_req_t0 = time.monotonic()
        self.mem_diag_enabled = False
        self.accumulated_tokens = 0
        self.max_param_retry_count = 3
        self.tool_start_callback = None
        self.token_update_callback = None
        self.emitted = []

    def emit(self, signal_name, *args):
        self.emitted.append((signal_name, args))

    def get_reasoning_content(self):
        return self.reasoning_content

    def extract_thought_signature(self, tc):
        return ""


def _content_batches(ctx) -> list:
    return [args[0] for name, args in ctx.emitted if name == "content_received"]


@pytest.fixture(scope="module")
def mod():
    return _load()


def test_threshold_119_deferred_then_flushed(mod):
    """恰 119 字（<120）：同步 consume 内不触发字数发射，结束冲刷一次性发出。"""
    text = "A" * 119
    ctx = _Ctx()
    events = [
        mod.StreamEvent(type="content_delta", text=text),
        mod.StreamEvent(type="finish", finish_reason="stop"),
    ]
    mod.OpenAIChatStreamSink().consume(events, ctx)
    batches = _content_batches(ctx)
    assert "".join(batches) == text
    assert batches[-1] == text, f"119 字应在结束冲刷一次性发出: {batches}"


def test_threshold_120_emits_immediately(mod):
    """恰 120 字（==120）：达阈值立即发射一批，不再等待。"""
    text = "A" * 120
    ctx = _Ctx()
    events = [
        mod.StreamEvent(type="content_delta", text=text),
        mod.StreamEvent(type="finish", finish_reason="stop"),
    ]
    mod.OpenAIChatStreamSink().consume(events, ctx)
    batches = _content_batches(ctx)
    assert "".join(batches) == text
    assert batches[0] == text, f"120 字应作为整批立即发射: {batches}"


def test_threshold_121_single_chunk_emits_whole(mod):
    """单 chunk 121 字：累加后 ≥120 即整批发射（批不硬截断 chunk）。"""
    text = "A" * 121
    ctx = _Ctx()
    events = [
        mod.StreamEvent(type="content_delta", text=text),
        mod.StreamEvent(type="finish", finish_reason="stop"),
    ]
    mod.OpenAIChatStreamSink().consume(events, ctx)
    batches = _content_batches(ctx)
    assert "".join(batches) == text
    assert batches == [text], f"单 chunk 121 字应整批发射（阈值是累加后判定，非截断）: {[len(b) for b in batches]}"


def test_threshold_cross_chunk_split(mod):
    """跨 chunk 聚合：60+60=120 达阈值整批发；再 10 字结束冲刷。"""
    ctx = _Ctx()
    events = [
        mod.StreamEvent(type="content_delta", text="A" * 60),
        mod.StreamEvent(type="content_delta", text="B" * 60),
        mod.StreamEvent(type="content_delta", text="C" * 10),
        mod.StreamEvent(type="finish", finish_reason="stop"),
    ]
    mod.OpenAIChatStreamSink().consume(events, ctx)
    batches = _content_batches(ctx)
    assert "".join(batches) == "A" * 60 + "B" * 60 + "C" * 10
    assert batches == ["A" * 60 + "B" * 60, "C" * 10], f"120 阈值应在前两 chunk 聚合后触发: {[len(b) for b in batches]}"


def test_multi_batch_ordering_and_completeness(mod):
    """300 字多 delta 多批：发射序列拼接 == 输入全文（完整 + 有序）。"""
    parts = ["甲" * 50, "乙" * 100, "丙" * 150]
    full = "".join(parts)
    ctx = _Ctx()
    events = [mod.StreamEvent(type="content_delta", text=p) for p in parts]
    events.append(mod.StreamEvent(type="finish", finish_reason="stop"))
    mod.OpenAIChatStreamSink().consume(events, ctx)
    batches = _content_batches(ctx)
    assert "".join(batches) == full, "批聚合必须无损有序"


def test_fence_cross_batch_integrity(mod):
    """围栏内容 >120 字跨批：sink 只保证文本流完整，切点任意合法（下游容忍）。"""
    code = "\n".join(f"line_{i} = {i}  # 注释内容填充长度" for i in range(12))
    payload = "```python\n" + code + "\n```"
    assert len(payload) > 120
    # 按 40 字一片喂入，模拟网络 chunk 任意切点
    pieces = [payload[i : i + 40] for i in range(0, len(payload), 40)]
    ctx = _Ctx()
    events = [mod.StreamEvent(type="content_delta", text=p) for p in pieces]
    events.append(mod.StreamEvent(type="finish", finish_reason="stop"))
    mod.OpenAIChatStreamSink().consume(events, ctx)
    batches = _content_batches(ctx)
    assert "".join(batches) == payload, "围栏跨批后文本流必须逐字节一致"


def test_slow_stream_80ms_flush_independent(mod):
    """慢流（<120 字/80ms）：80ms 保底发射独立生效，不滞留。"""
    ctx = _Ctx()
    events = [
        mod.StreamEvent(type="content_delta", text="短句一。"),
    ]
    sink = mod.OpenAIChatStreamSink()

    class _TimedEvents:
        """第一个 delta 消费后睡眠 90ms 再吐第二个，制造 >80ms 批间隔。"""

        def __init__(self):
            self._seq = [events[0], None, mod.StreamEvent(type="finish", finish_reason="stop")]

        def __iter__(self):
            return self

        def __next__(self):
            if not self._seq:
                raise StopIteration
            item = self._seq.pop(0)
            if item is None:
                time.sleep(0.09)
                return self._seq.pop(0)
            return item

    sink.consume(_TimedEvents(), ctx)
    batches = _content_batches(ctx)
    assert "".join(batches) == "短句一。"
    assert batches[0] == "短句一。", f"80ms 保底应发射首批: {batches}"
